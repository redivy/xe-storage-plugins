#!/usr/bin/python
# Copyright (C) 2006-2007 XenSource Ltd.
# Copyright (C) 2008-2009 Citrix Ltd.
#
# This program is free software; you can redistribute it and/or modify 
# it under the terms of the GNU Lesser General Public License as published 
# by the Free Software Foundation; version 2.1 only.
#
# This program is distributed in the hope that it will be useful, 
# but WITHOUT ANY WARRANTY; without even the implied warranty of 
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the 
# GNU Lesser General Public License for more details.
#
# LVHDSR: VHD on LVM storage repository
#

import SR
import VDI
import SRCommand
import util
import lvutil
import lvmcache
import vhdutil
import lvhdutil
import scsiutil
import metadata

import time
import os, sys
import xml.dom.minidom
import errno
import xs_errors
import cleanup
from journaler import Journaler
from lock import Lock
from refcounter import RefCounter
from ipc import IPCFlag
from lvmanager import LVActivator
import XenAPI

geneology = {}
CAPABILITIES = ["SR_PROBE","SR_UPDATE",
        "VDI_CREATE","VDI_DELETE","VDI_ATTACH", "VDI_DETACH",
        "VDI_CLONE", "VDI_SNAPSHOT", "VDI_RESIZE"]

CONFIGURATION = [ ['device', 'local device path (required) (e.g. /dev/sda3)'] ]


DRIVER_INFO = {
    'name': 'Local VHD on LVM',
    'description': 'SR plugin which represents disks as VHD disks on ' + \
            'Logical Volumes within a locally-attached Volume Group',
    'vendor': 'XenSource Inc',
    'copyright': '(C) 2008 XenSource Inc',
    'driver_version': '1.0',
    'required_api_version': '1.0',
    'capabilities': CAPABILITIES,
    'configuration': CONFIGURATION
    }

PARAM_VHD = "vhd"
PARAM_RAW = "raw"


def locking(excType, override=True):
    def locking2(op):
        def wrapper(self, *args):
            self.lock.acquire()
            try:
                try:
                    ret = op(self, *args)
                except (util.SMException, XenAPI.Failure), e:
                    util.logException("LVHD:%s" % op)
                    msg = str(e)
                    if isinstance(e, util.CommandException):
                        msg = "Command %s failed (%s): %s" % \
                                (e.cmd, e.code, e.reason)
                    if override:
                        raise xs_errors.XenError(excType, opterr=msg)
                    else:
                        raise
                except:
                    util.logException("LVHD:%s" % op)
                    raise
            finally:
                self.lock.release()
                # we don't need to hold the lock to dec refcounts of activated 
                # LVs
                success = self.lvActivator.deactivateAll()
            if not success:
                raise xs_errors.XenError("SMGeneral", opterr="deactivation")
            return ret
        return wrapper
    return locking2



class LVHDSR(SR.SR):
    DRIVER_TYPE = 'lvhd'

    THIN_PROVISIONING_DEFAULT = False
    THIN_PLUGIN = "lvhd-thin"

    PLUGIN_ON_SLAVE = "on-slave"

    FLAG_USE_VHD = "use_vhd"
    MDVOLUME_NAME = "MGT"

    LOCK_RETRY_INTERVAL = 3
    LOCK_RETRY_ATTEMPTS = 10

    TEST_MODE_KEY = "testmode"
    TEST_MODE_VHD_FAIL_REPARENT_BEGIN   = "vhd_fail_reparent_begin"
    TEST_MODE_VHD_FAIL_REPARENT_LOCATOR = "vhd_fail_reparent_locator"
    TEST_MODE_VHD_FAIL_REPARENT_END     = "vhd_fail_reparent_end"
    TEST_MODE_VHD_FAIL_RESIZE_BEGIN     = "vhd_fail_resize_begin"
    TEST_MODE_VHD_FAIL_RESIZE_DATA      = "vhd_fail_resize_data"
    TEST_MODE_VHD_FAIL_RESIZE_METADATA  = "vhd_fail_resize_metadata"
    TEST_MODE_VHD_FAIL_RESIZE_END       = "vhd_fail_resize_end"

    ENV_VAR_VHD_TEST = {
            TEST_MODE_VHD_FAIL_REPARENT_BEGIN:
                "VHD_UTIL_TEST_FAIL_REPARENT_BEGIN",
            TEST_MODE_VHD_FAIL_REPARENT_LOCATOR:
                "VHD_UTIL_TEST_FAIL_REPARENT_LOCATOR",
            TEST_MODE_VHD_FAIL_REPARENT_END:
                "VHD_UTIL_TEST_FAIL_REPARENT_END",
            TEST_MODE_VHD_FAIL_RESIZE_BEGIN:
                "VHD_UTIL_TEST_FAIL_RESIZE_BEGIN",
            TEST_MODE_VHD_FAIL_RESIZE_DATA:
                "VHD_UTIL_TEST_FAIL_RESIZE_DATA_MOVED",
            TEST_MODE_VHD_FAIL_RESIZE_METADATA: 
                "VHD_UTIL_TEST_FAIL_RESIZE_METADATA_MOVED",
            TEST_MODE_VHD_FAIL_RESIZE_END:
                "VHD_UTIL_TEST_FAIL_RESIZE_END"
    }
    testMode = ""

    legacyMode = True

    def handles(type):
        """Returns True if this SR class understands the given dconf string"""
        # we can pose as LVMSR or EXTSR for compatibility purposes
        if sys.argv[0].endswith("LVMSR"):
            return type == "lvm"
        elif sys.argv[0].endswith("EXTSR"):
            return type == "ext"
        return type == LVHDSR.DRIVER_TYPE
    handles = staticmethod(handles)

    def load(self, sr_uuid):
        if not self.dconf.has_key('device') or not self.dconf['device']:
            raise xs_errors.XenError('ConfigDeviceMissing',)
        self.root = self.dconf['device']
        for dev in self.root.split(','):
            if not self._isvalidpathstring(dev):
                raise xs_errors.XenError('ConfigDeviceInvalid', \
                        opterr='path is %s' % dev)

        self.isMaster = False
        if self.dconf.has_key('SRmaster') and self.dconf['SRmaster'] == 'true':
            self.isMaster = True

        self.lock = Lock(vhdutil.LOCK_TYPE_SR, self.uuid)
        self.sr_vditype = SR.DEFAULT_TAP
        self.uuid = sr_uuid
        self.vgname = lvhdutil.VG_PREFIX + self.uuid
        self.path = os.path.join(lvhdutil.VG_LOCATION, self.vgname)
        self.mdpath = os.path.join(self.path, self.MDVOLUME_NAME)
        self.thinpr = self.THIN_PROVISIONING_DEFAULT
        try:
            self.lvmCache = lvmcache.LVMCache(self.vgname)
        except:
            raise xs_errors.XenError('SRUnavailable', \
                        opterr='Failed to initialise the LVMCache')
        self.lvActivator = LVActivator(self.uuid, self.lvmCache)
        self.journaler = Journaler(self.lvmCache)
        if not self.srcmd.params.get("sr_ref"):
            return # must be a probe call
        # Test for thick vs thin provisioning conf parameter
        if self.dconf.has_key('allocation'):
            alloc = self.dconf['allocation']
            if alloc == 'thin':
                self.thinpr = True
            elif alloc == 'thick':
                self.thinpr = False
            else:
                raise xs_errors.XenError('InvalidArg', \
                        opterr='Allocation parameter must be either thick or thin')

        self.other_conf = self.session.xenapi.SR.get_other_config(self.sr_ref)
        if self.other_conf.get(self.TEST_MODE_KEY):
            self.testMode = self.other_conf[self.TEST_MODE_KEY]
            self._prepareTestMode()

        self.sm_config = self.session.xenapi.SR.get_sm_config(self.sr_ref)
        # sm_config flag overrides PBD
        if self.sm_config.get('allocation') == "thick":
            self.thinpr = False
        elif self.sm_config.get('allocation') == "thin":
            self.thinpr = True

        if self.sm_config.get(self.FLAG_USE_VHD) == "true":
            self.legacyMode = False

        if lvutil._checkVG(self.vgname):
            if self.isMaster and not self.cmd in ["vdi_attach", "vdi_detach"]:
                self._undoAllJournals()
            if not self.cmd in ["sr_attach","sr_probe"]:
                self._checkMetadataVolume(self.sm_config)

    def _checkMetadataVolume(self, map):
        exists = self.lvmCache.checkLV(self.MDVOLUME_NAME)
        if self.isMaster:
            if exists and self.cmd == "sr_attach":
                self._synchMetaData(map)
            elif not exists and map.get(self.FLAG_USE_VHD) == "true":
                self._introduceMetaDataVolume(map)
        if exists:
            self.legacyMode = False

    def _synchMetaData(self, map):
        util.SMlog("Synching Metadata volume")
        try:
            self.lvmCache.activateNoRefcount(self.MDVOLUME_NAME)
            xml = metadata.retrieveXMLfromFile(self.mdpath)
            Dict = metadata._parseXML(xml)
            if Dict.get("allocation") == 'thick':
                self.thinpr = False
                map['allocation'] = 'thick'
            else:
                self.thinpr = True
                map['allocation'] = 'thin'
            self.lvmCache.deactivateNoRefcount(self.MDVOLUME_NAME)
        except:
            raise
            raise xs_errors.XenError('MetadataError', \
                         opterr='Error synching Metadata Volume')
        try:
            map[self.FLAG_USE_VHD] = 'true'
            self.session.xenapi.SR.set_sm_config(self.sr_ref, map)
        except:
            raise xs_errors.XenError('MetadataError', \
                         opterr='Error updating sm_config key')

        
    def _introduceMetaDataVolume(self, map):
        util.SMlog("Creating Metadata volume")
        try:
            self.lvmCache.create(self.MDVOLUME_NAME, 4*1024*1024)
            if self.thinpr:
                allocstr = "thin"
            else:
                allocstr = "thick"
            Dict = {'allocation':allocstr}
            metadata.writeXMLtoFile(self.mdpath, Dict)
            map[self.FLAG_USE_VHD] = "true"
            map['allocation'] = allocstr
            self.session.xenapi.SR.set_sm_config(self.sr_ref, map)
            self.lvmCache.deactivateNoRefcount(self.MDVOLUME_NAME)
        except:
            raise xs_errors.XenError('MetadataError', \
                         opterr='Error introducing Metadata Volume')

    def _removeMetadataVolume(self):
        exists = self.lvmCache.checkLV(self.MDVOLUME_NAME)
        if exists:
            try:
                self.lvmCache.remove(self.MDVOLUME_NAME)
            except:
                raise xs_errors.XenError('MetadataError', \
                             opterr='Failed to delete MGT Volume')       

    @locking("SRUnavailable")
    def create(self, uuid, size):
        util.SMlog("LVHDSR.create for %s" % self.uuid)
        if not self.isMaster:
            util.SMlog('sr_create blocked for non-master')
            raise xs_errors.XenError('LVMMaster')

        if lvutil._checkVG(self.vgname):
            raise xs_errors.XenError('SRExists')

        # Check none of the devices already in use by other PBDs
        if util.test_hostPBD_devs(self.session, self.root):
            raise xs_errors.XenError('SRInUse')

        # Check serial number entry in SR records
        for dev in self.root.split(','):
            if util.test_scsiserial(self.session, dev):
                raise xs_errors.XenError('SRInUse')

        lvutil.createVG(self.root, self.vgname)

        #Update serial number string
        scsiutil.add_serial_record(self.session, self.sr_ref, \
                scsiutil.devlist_to_serialstring(self.root.split(',')))

        # create the metadata volume; its absence => SR in legacy mode
        self._introduceMetaDataVolume(self.sm_config)

    @locking("SRUnavailable")
    def delete(self, uuid):
        util.SMlog("LVHDSR.delete for %s" % self.uuid)
        if not self.isMaster:
            raise xs_errors.XenError('LVMMaster')
        cleanup.gc_force(self.session, self.uuid)

        self._removeMetadataVolume()
        self.lvmCache.refresh()
        if len(lvhdutil.getLVInfo(self.lvmCache)) > 0:
            raise xs_errors.XenError('SRNotEmpty')

        lvutil.removeVG(self.root, self.vgname)
        self._cleanup()

    @locking("SRUnavailable")
    def attach(self, uuid):
        util.SMlog("LVHDSR.attach for %s" % self.uuid)
        self._cleanup(True) # in case of host crashes, if detach wasn't called

        if not util.match_uuid(self.uuid) or not lvutil._checkVG(self.vgname):
            raise xs_errors.XenError('SRUnavailable', \
                    opterr='no such volume group: %s' % self.vgname)

        # Refresh the metadata status
        self._checkMetadataVolume(self.sm_config)

        if self.isMaster:
            # Probe for LUN resize
            for dev in self.root.split(','):
                lvutil.refreshPV(dev)

            #Update SCSIid string
            util.SMlog("Calling devlist_to_serial")
            scsiutil.add_serial_record(self.session, self.sr_ref, \
                    scsiutil.devlist_to_serialstring(self.root.split(',')))

        # Test Legacy Mode Flag and update if VHD volumes exist
        if self.isMaster and self.legacyMode:
            vdiInfo = lvhdutil.getVDIInfo(self.lvmCache)
            for uuid, info in vdiInfo.iteritems():
                if info.vdiType == lvhdutil.VDI_TYPE_VHD:
                    self.legacyMode = False
                    map = self.session.xenapi.SR.get_sm_config(self.sr_ref)
                    self._introduceMetaDataVolume(map)
                    break
                        

        # Set the block scheduler
        for dev in self.root.split(','): self.block_setscheduler(dev)
            
    @locking("SRUnavailable")
    def detach(self, uuid):
        util.SMlog("LVHDSR.detach for %s" % self.uuid)
        cleanup.abort(self.uuid)
        if not lvutil._checkVG(self.vgname):
            return

        # Deactivate any active LVs
        lvutil.setActiveVG(self.path, False)
        # Don't delete lock files on the master as it will break the locking 
        # between SM and any GC thread that survives through SR.detach.  
        # However, we should still delete lock files on slaves as it is the 
        # only place to do so.
        self._cleanup(self.isMaster)

    @locking("SRScan")
    def scan(self, uuid):
        util.SMlog("LVHDSR.scan for %s" % self.uuid)
        if not self.isMaster:
            util.SMlog('sr_scan blocked for non-master')
            raise xs_errors.XenError('LVMMaster')

        self._loadvdis()
        stats = lvutil._getVGstats(self.vgname)
        self.physical_size = stats['physical_size']
        self.physical_utilisation = stats['physical_utilisation']

        ret = super(LVHDSR, self).scan(uuid)
        self._kickGC()
        return ret

    @locking("SRUnavailable")
    def update(self, uuid):
        if not lvutil._checkVG(self.vgname):
            return
        self._updateStats(uuid, 0)

    def _updateStats(self, uuid, virtAllocDelta):
        valloc = int(self.session.xenapi.SR.get_virtual_allocation(self.sr_ref))
        self.virtual_allocation = valloc + virtAllocDelta
        stats = lvutil._getVGstats(self.vgname)
        self.physical_size = stats['physical_size']
        self.physical_utilisation = stats['physical_utilisation']
        self._db_update()        

    def probe(self):
        return lvutil.srlist_toxml(\
                lvutil.scan_srlist(lvhdutil.VG_PREFIX, self.root))

    def vdi(self, uuid):
        return LVHDVDI(self, uuid)

    def _loadvdis(self):
        self.virtual_allocation = 0
        self.vdiInfo = lvhdutil.getVDIInfo(self.lvmCache)
        self.allVDIs = {}

        for uuid, info in self.vdiInfo.iteritems():
            if uuid.startswith(cleanup.SR.TMP_RENAME_PREFIX):
                continue
            if info.scanError:
                raise xs_errors.XenError('VDIUnavailable', \
                        opterr='Error scanning VDI %s' % uuid)
            self.vdis[uuid] = self.allVDIs[uuid] = self.vdi(uuid)
            if not self.vdis[uuid].hidden:
                self.virtual_allocation += self.vdis[uuid].utilisation

        for uuid, vdi in self.vdis.iteritems():
            if vdi.parent:
                if self.vdis.has_key(vdi.parent):
                    self.vdis[vdi.parent].read_only = True
                if geneology.has_key(vdi.parent):
                    geneology[vdi.parent].append(uuid)
                else:
                    geneology[vdi.parent] = [uuid]
                
        # Now remove all hidden leaf nodes to avoid introducing records that
        # will be GC'ed
        for uuid in self.vdis.keys():
            if not geneology.has_key(uuid) and self.vdis[uuid].hidden:
                util.SMlog("Scan found hidden leaf (%s), ignoring" % uuid)
                del self.vdis[uuid]

    def _ensureSpaceAvailable(self, amount_needed):
        space_available = lvutil._getVGstats(self.vgname)['freespace']
        if (space_available < amount_needed):
            util.SMlog("Not enough space! free space: %d, need: %d" % \
                    (space_available, amount_needed))
            raise xs_errors.XenError('SRNoSpace')

    def _handleInterruptedCloneOps(self):
        entries = self.journaler.getAll(LVHDVDI.JRN_CLONE)
        for uuid, val in entries.iteritems():
            util.fistpoint.activate("LVHDRT_clone_vdi_before_undo_clone",self.uuid)
            self._handleInterruptedCloneOp(uuid, val)
            util.fistpoint.activate("LVHDRT_clone_vdi_after_undo_clone",self.uuid)
            self.journaler.remove(LVHDVDI.JRN_CLONE, uuid)

    def _handleInterruptedCoalesceLeaf(self):
        entries = self.journaler.getAll(cleanup.VDI.JRN_LEAF)
        if len(entries) > 0:
            util.SMlog("*** INTERRUPTED COALESCE-LEAF OP DETECTED ***")
            cleanup.gc(self.session, self.uuid, False)
            self.lvmCache.refresh()

    def _handleInterruptedCloneOp(self, origUuid, jval, forceUndo = False):
        """Either roll back or finalize the interrupted snapshot/clone
        operation. Rolling back is unsafe if the leaf VHDs have already been
        in use and written to. However, it is always safe to roll back while
        we're still in the context of the failed snapshot operation since the
        VBD is paused for the duration of the operation"""
        util.SMlog("*** INTERRUPTED CLONE OP: for %s (%s)" % (origUuid, jval))
        lvs = lvhdutil.getLVInfo(self.lvmCache)
        baseUuid, clonUuid = jval.split("_")

        # is there a "base copy" VDI?
        if not lvs.get(baseUuid):
            # no base copy: make sure the original is there
            if lvs.get(origUuid):
                util.SMlog("*** INTERRUPTED CLONE OP: nothing to do")
                return
            raise util.SMException("base copy %s not present, "\
                    "but no original %s found" % (baseUuid, origUuid))

        if forceUndo:
            util.SMlog("Explicit revert")
            self._undoCloneOp(lvs, origUuid, baseUuid, clonUuid)
            return

        if not lvs.get(origUuid) or (clonUuid and not lvs.get(clonUuid)):
            util.SMlog("One or both leaves missing => revert")
            self._undoCloneOp(lvs, origUuid, baseUuid, clonUuid)
            return

        vdis = lvhdutil.getVDIInfo(self.lvmCache)
        if vdis[origUuid].scanError or (clonUuid and vdis[clonUuid].scanError):
            util.SMlog("One or both leaves invalid => revert")
            self._undoCloneOp(lvs, origUuid, baseUuid, clonUuid)
            return

        orig = vdis[origUuid]
        base = vdis[baseUuid]
        self.lvActivator.activate(baseUuid, base.lvName, False)
        self.lvActivator.activate(origUuid, orig.lvName, False)
        if orig.parentUuid != baseUuid:
            parent = vdis[orig.parentUuid]
            self.lvActivator.activate(parent.uuid, parent.lvName, False)
        origPath = os.path.join(self.path, orig.lvName)
        if not vhdutil.check(origPath):
            util.SMlog("Orig VHD invalid => revert")
            self._undoCloneOp(lvs, origUuid, baseUuid, clonUuid)
            return

        if clonUuid:
            clon = vdis[clonUuid]
            clonPath = os.path.join(self.path, clon.lvName)
            self.lvActivator.activate(clonUuid, clon.lvName, False)
            if not vhdutil.check(clonPath):
                util.SMlog("Clon VHD invalid => revert")
                self._undoCloneOp(lvs, origUuid, baseUuid, clonUuid)
                return

        util.SMlog("Snapshot appears valid, will not roll back")
        self._completeCloneOp(vdis, origUuid, baseUuid, clonUuid)

    def _undoCloneOp(self, lvs, origUuid, baseUuid, clonUuid):
        base = lvs[baseUuid]
        basePath = os.path.join(self.path, base.name)

        # make the parent RW
        if base.readonly:
            self.lvmCache.setReadonly(base.name, False)

        ns = lvhdutil.NS_PREFIX_LVM + self.uuid
        origRefcountBinary = RefCounter.check(origUuid, ns)[1]
        origRefcountNormal = 0

        # un-hide the parent
        if base.vdiType == lvhdutil.VDI_TYPE_VHD:
            self.lvActivator.activate(baseUuid, base.name, False)
            origRefcountNormal = 1
            vhdInfo = vhdutil.getVHDInfo(basePath, lvhdutil.extractUuid, False)
        if base.vdiType == lvhdutil.VDI_TYPE_VHD and vhdInfo.hidden:
            vhdutil.setHidden(basePath, False)
        elif base.vdiType == lvhdutil.VDI_TYPE_RAW and base.hidden:
            self.lvmCache.setHidden(base.name, False)

        # inflate the parent to fully-allocated size
        if base.vdiType == lvhdutil.VDI_TYPE_VHD:
            fullSize = lvhdutil.calcSizeVHDLV(vhdInfo.sizeVirt)
            lvhdutil.inflate(self.journaler, self.uuid, baseUuid, fullSize)

        # remove the child nodes
        if clonUuid and lvs.get(clonUuid):
            if lvs[clonUuid].vdiType != lvhdutil.VDI_TYPE_VHD:
                raise util.SMException("clone %s not VHD" % clonUuid)
            self.lvmCache.remove(lvs[clonUuid].name)
            if self.lvActivator.get(clonUuid, False):
                self.lvActivator.remove(clonUuid, False)
        if lvs.get(origUuid):
            self.lvmCache.remove(lvs[origUuid].name)

        # rename back
        origLV = lvhdutil.LV_PREFIX[base.vdiType] + origUuid
        self.lvmCache.rename(base.name, origLV)
        RefCounter.reset(baseUuid, ns)
        if self.lvActivator.get(baseUuid, False):
            self.lvActivator.replace(baseUuid, origUuid, origLV, False)
        RefCounter.set(origUuid, origRefcountNormal, origRefcountBinary, ns)
        util.SMlog("*** INTERRUPTED CLONE OP: rollback success")

    def _completeCloneOp(self, vdis, origUuid, baseUuid, clonUuid):
        """Finalize the interrupted snapshot/clone operation. This must not be
        called from the live snapshot op context because we attempt to pause/
        unpause the VBD here (the VBD is already paused during snapshot, so it
        would cause a deadlock)"""
        base = vdis[baseUuid]
        clon = None
        if clonUuid:
            clon = vdis[clonUuid]

        cleanup.abort(self.uuid)

        # make sure the parent is hidden and read-only
        if not base.hidden:
            if base.vdiType == lvhdutil.VDI_TYPE_RAW:
                self.lvmCache.setHidden(base.lvName)
            else:
                basePath = os.path.join(self.path, base.lvName)
                vhdutil.setHidden(basePath)
        if not base.lvReadonly:
            self.lvmCache.setReadonly(base.lvName, True)

        # NB: since this snapshot-preserving call is only invoked outside the 
        # snapshot op context, we assume the LVM metadata on the involved slave 
        # has by now been refreshed and do not attempt to do it here

        # Update the original record
        try:
            vdi_ref = self.session.xenapi.VDI.get_by_uuid(origUuid)
            sm_config = self.session.xenapi.VDI.get_sm_config(vdi_ref)
            sm_config["vdi_type"] = lvhdutil.VDI_TYPE_VHD
            sm_config['vhd-parent'] = baseUuid
            self.session.xenapi.VDI.set_sm_config(vdi_ref, sm_config)
        except XenAPI.Failure:
            util.SMlog("ERROR updating the orig record")

        # introduce the new VDI records
        if clonUuid:
            try:
                clon_vdi = VDI.VDI(self, clonUuid)
                clon_vdi.read_only = False
                clon_vdi.location = clonUuid
                clon_vdi.utilisation = clon.sizeLV
                clon_vdi.sm_config = {
                        "vdi_type": lvhdutil.VDI_TYPE_VHD,
                        "vhd-parent": baseUuid }
                clon_vdi_ref = clon_vdi._db_introduce()
                util.SMlog("introduced clon VDI: %s (%s)" % \
                        (clon_vdi_ref, clonUuid))
            except XenAPI.Failure:
                util.SMlog("ERROR introducing the clon record")

        try:
            base_vdi = VDI.VDI(self, baseUuid) # readonly parent
            base_vdi.label = "base copy"
            base_vdi.read_only = True
            base_vdi.location = baseUuid
            base_vdi.size = base.sizeVirt
            base_vdi.utilisation = base.sizeLV
            base_vdi.managed = False
            base_vdi.sm_config = {
                    "vdi_type": lvhdutil.VDI_TYPE_VHD,
                    "vhd-parent": baseUuid }
            base_vdi_ref = base_vdi._db_introduce()
            util.SMlog("introduced base VDI: %s (%s)" % \
                    (base_vdi_ref, baseUuid))
        except XenAPI.Failure:
            util.SMlog("ERROR introducing the base record")

        util.SMlog("*** INTERRUPTED CLONE OP: complete")

    @locking("SMGeneral")
    def _undoAllJournals(self):
        """Undo all VHD & SM interrupted journaled operations. This call must
        be serialized with respect to all operations that create journals"""
        # undoing interrupted inflates must be done first, since undoing VHD 
        # ops might require inflations
        self._undoAllInflateJournals()
        self._undoAllVHDJournals()
        self._handleInterruptedCloneOps()
        self._handleInterruptedCoalesceLeaf()

    def _undoAllInflateJournals(self):
        entries = self.journaler.getAll(lvhdutil.JRN_INFLATE)
        if len(entries) == 0:
            return
        self._loadvdis()
        for uuid, val in entries.iteritems():
            vdi = self.vdis.get(uuid)
            if vdi:
                util.SMlog("Found inflate journal %s, deflating %s to %s" % \
                        (uuid, vdi.path, val))
                if vdi.readonly:
                    self.lvmCache.setReadonly(vdi.lvname, False)
                self.lvActivator.activate(uuid, vdi.lvname, False)
                currSizeLV = self.lvmCache.getSize(vdi.lvname)
                util.zeroOut(vdi.path, currSizeLV - vhdutil.VHD_FOOTER_SIZE,
                        vhdutil.VHD_FOOTER_SIZE)
                lvhdutil.deflate(self.lvmCache, vdi.lvname, int(val))
                if vdi.readonly:
                    self.lvmCache.setReadonly(vdi.lvname, True)                
            self.journaler.remove(lvhdutil.JRN_INFLATE, uuid)
        delattr(self,"vdiInfo")
        delattr(self,"allVDIs")

    def _undoAllVHDJournals(self):
        """check if there are VHD journals in existence and revert them"""
        journals = lvhdutil.getAllVHDJournals(self.lvmCache)
        if len(journals) == 0:
            return
        self._loadvdis()
        for uuid, jlvName in journals:
            vdi = self.vdis[uuid]
            util.SMlog("Found VHD journal %s, reverting %s" % (uuid, vdi.path))
            self.lvActivator.activate(uuid, vdi.lvname, False)
            self.lvmCache.activateNoRefcount(jlvName)
            fullSize = lvhdutil.calcSizeVHDLV(vdi.size)
            lvhdutil.inflate(self.journaler, self.uuid, vdi.uuid, fullSize)
            try:
                jFile = os.path.join(self.path, jlvName)
                vhdutil.revert(vdi.path, jFile)
            except util.CommandException:
                util.logException("VHD journal revert")
                vhdutil.check(vdi.path)
                util.SMlog("VHD revert failed but VHD ok: removing journal")
            # Attempt to reclaim unused space
            vhdInfo = vhdutil.getVHDInfo(vdi.path, lvhdutil.extractUuid, False)
            NewSize = lvhdutil.calcSizeVHDLV(vhdInfo.sizeVirt)
            if NewSize < fullSize:
                lvhdutil.deflate(self.lvmCache, vdi.lvname, int(NewSize))            
            
            self.lvmCache.remove(jlvName)
            # VDI will remain inflated
        delattr(self,"vdiInfo")
        delattr(self,"allVDIs")

    def _updateSlavesOnClone(self, activeVBDs, origOldLV, origLV,
            baseUuid, baseLV):
        """We need to reactivate the original LV on each slave (note that the
        name for the original LV might change), as well as init the refcount
        for the base LV"""
        slaves = util.get_hosts(self.session, activeVBDs)
        pool = self.session.xenapi.pool.get_all()[0]
        master = self.session.xenapi.pool.get_master(pool)
        if master in slaves:
            slaves.remove(master)
        if len(slaves) == 0:
            return

        util.SMlog("Updating %s, %s, %s on %d slaves:" % \
                (origOldLV, origLV, baseLV, len(slaves)))
        ns = lvhdutil.NS_PREFIX_LVM + self.uuid
        for slave in slaves:
            util.SMlog("Updating refcount on slave %s" % slave)
            args = {"vgName" : self.vgname,
                    "action1": "deactivateNoRefcount",
                    "lvName1": origOldLV,
                    "action2": "refresh",
                    "lvName2": origLV,
                    "action3": "activate",
                    "ns3"    : ns,
                    "lvName3": baseLV,
                    "uuid3"  : baseUuid}
            text = self.session.xenapi.host.call_plugin( \
                    slave, self.PLUGIN_ON_SLAVE, "multi", args)
            util.SMlog("call-plugin returned: '%s'" % text)

    def _cleanup(self, skipLockCleanup = False):
        """delete stale refcounter, flag, and lock files"""
        RefCounter.resetAll(lvhdutil.NS_PREFIX_LVM + self.uuid)
        IPCFlag(self.uuid).clearAll()
        if not skipLockCleanup:
            Lock.cleanupAll(self.uuid)
            Lock.cleanupAll(lvhdutil.NS_PREFIX_LVM + self.uuid)

    def _prepareTestMode(self):
        util.SMlog("Test mode: %s" % self.testMode)
        if self.ENV_VAR_VHD_TEST.get(self.testMode):
            os.environ[self.ENV_VAR_VHD_TEST[self.testMode]] = "yes"
            util.SMlog("Setting env %s" % self.ENV_VAR_VHD_TEST[self.testMode])

    def _kickGC(self):
        # don't bother if an instance already running (this is just an 
        # optimization to reduce the overhead of forking a new process if we 
        # don't have to, but the process will check the lock anyways)
        lockRunning = Lock(cleanup.LOCK_TYPE_RUNNING, self.uuid) 
        if not lockRunning.acquireNoblock():
            if cleanup.should_preempt(self.session, self.uuid):
                util.SMlog("Aborting currently-running coalesce of garbage VDI")
                cleanup.abort(self.uuid)
            else:
                util.SMlog("A GC instance already running, not kicking")
                return
        else:
            lockRunning.release()

        util.SMlog("Kicking GC")
        cleanup.gc(self.session, self.uuid, True)


class LVHDVDI(VDI.VDI):

    MAX_VDI_SIZE_MB = 2 * 1024 * 1024
    VHD_SIZE_INC = 2 * 1024 * 1024
    MIN_VIRT_SIZE = 2 * 1024 * 1024

    SNAPSHOT_SINGLE = 1 # true snapshot: 1 leaf, 1 read-only parent
    SNAPSHOT_DOUBLE = 2 # regular snapshot/clone that creates 2 leaves

    JRN_CLONE = "clone" # journal entry type for the clone operation

    def load(self, vdi_uuid):
        self.lock = self.sr.lock
        self.lvActivator = self.sr.lvActivator
        self.loaded   = False
        self.vdi_type = lvhdutil.VDI_TYPE_VHD
        if self.sr.legacyMode or util.fistpoint.is_active("xenrt_default_vdi_type_legacy"):
            self.vdi_type = lvhdutil.VDI_TYPE_RAW
        self.uuid     = vdi_uuid
        self.location = self.uuid
        self.exists   = True

        if hasattr(self.sr, "vdiInfo") and self.sr.vdiInfo.get(self.uuid):
            self._initFromVDIInfo(self.sr.vdiInfo[self.uuid])
            if self.parent:
                self.sm_config_override['vhd-parent'] = self.parent
            else:
                self.sm_config_override['vhd-parent'] = None
            return
        
        # scan() didn't run: determine the type of the VDI manually
        if self._determineType():
            return

        # the VDI must be in the process of being created
        self.exists = False
        if self.sr.srcmd.params.has_key("vdi_sm_config") and \
                self.sr.srcmd.params["vdi_sm_config"].has_key("type"):
            type = self.sr.srcmd.params["vdi_sm_config"]["type"]
            if type == PARAM_RAW:
                self.vdi_type = lvhdutil.VDI_TYPE_RAW
            elif type == PARAM_VHD:
                self.vdi_type = lvhdutil.VDI_TYPE_VHD
                if self.sr.cmd == 'vdi_create' and self.sr.legacyMode:
                    raise xs_errors.XenError('VDICreate', \
                        opterr='Cannot create VHD type disk in legacy mode')
            else:
                raise xs_errors.XenError('VDICreate', opterr='bad type')
        self.lvname = "%s%s" % (lvhdutil.LV_PREFIX[self.vdi_type], vdi_uuid)
        self.path = os.path.join(self.sr.path, self.lvname)

    @locking("VDICreate")
    def create(self, sr_uuid, vdi_uuid, size):
        util.SMlog("LVHDVDI.create for %s" % self.uuid)
        if not self.sr.isMaster:
            raise xs_errors.XenError('LVMMaster')
        if self.exists:
            raise xs_errors.XenError('VDIExists')

        if size / 1024 / 1024 > self.MAX_VDI_SIZE_MB:
            raise xs_errors.XenError('VDISize',
                    opterr="VDI size cannot exceed %d MB" % \
                            self.MAX_VDI_SIZE_MB)
        
        if size < self.MIN_VIRT_SIZE:
            size = self.MIN_VIRT_SIZE
        size = util.roundup(self.VHD_SIZE_INC, size)
        util.SMlog("LVHDVDI.create: type = %s, %s (size=%s)" %\
                (self.vdi_type, self.path, size))
        lvSize = 0
        if self.vdi_type == lvhdutil.VDI_TYPE_RAW:
            lvSize = util.roundup(lvutil.LVM_SIZE_INCREMENT, long(size))
        else:
            if self.sr.thinpr:
                lvSize = util.roundup(lvutil.LVM_SIZE_INCREMENT, 
                        vhdutil.calcOverheadEmpty(lvhdutil.MSIZE))
            else:
                lvSize = lvhdutil.calcSizeVHDLV(long(size))

        self.sm_config = self.sr.srcmd.params["vdi_sm_config"]
        self.sr._ensureSpaceAvailable(lvSize)

        self.sr.lvmCache.create(self.lvname, lvSize)
        if self.vdi_type == lvhdutil.VDI_TYPE_RAW:
            self.size = self.sr.lvmCache.getSize(self.lvname)
        else:
            vhdutil.create(self.path, long(size), False, lvhdutil.MSIZE_MB)
            self.size = vhdutil.getSizeVirt(self.path)
        self.sr.lvmCache.deactivateNoRefcount(self.lvname)

        self.utilisation = lvSize
        self.sm_config["vdi_type"] = self.vdi_type
        self._db_introduce()
        self.sr._updateStats(self.sr.uuid, self.size)
        return VDI.VDI.get_params(self)

    @locking("VDIDelete")
    def delete(self, sr_uuid, vdi_uuid):
        util.SMlog("LVHDVDI.delete for %s" % self.uuid)
        try:
            self._loadThis()
        except SR.SRException, e:
            # Catch 'VDI doesn't exist' exception
            if e.errno == 46:
                return
            raise

        vdi_ref = self.sr.srcmd.params['vdi_ref']
        if not self.session.xenapi.VDI.get_managed(vdi_ref):
            raise xs_errors.XenError("VDIDelete", \
                          opterr="Deleting non-leaf node not permitted")
            
        if not self.hidden:
            self._markHidden()
        self._db_forget()
        # deactivate here because it might be too late to do it in the "final" 
        # step: GC might have removed the LV by then
        if self.sr.lvActivator.get(self.uuid, False):
            self.sr.lvActivator.deactivate(self.uuid, False)

        self.sr._updateStats(self.sr.uuid, -self.size)
        self.sr._kickGC()

    def attach(self, sr_uuid, vdi_uuid):
        util.SMlog("LVHDVDI.attach for %s" % self.uuid)
        if self.sr.journaler.hasJournals(self.uuid):
            raise xs_errors.XenError('VDIUnavailable', 
                    opterr='Interrupted operation detected on this VDI, '
                    'scan SR first to trigger auto-repair')

        writable = ('args' not in self.sr.srcmd.params) or (self.sr.srcmd.params['args'][0] == "true")
        needInflate = True
        if self.vdi_type == lvhdutil.VDI_TYPE_RAW or not writable:
            needInflate = False
        else:
            self._loadThis()
            if self.utilisation >= lvhdutil.calcSizeVHDLV(self.size):
                needInflate = False

        if needInflate:
            try:
                self._prepareThin(True)
            except:
                util.logException("attach")
                raise xs_errors.XenError('LVMProvisionAttach')

        try:
            return self._attach()
        finally:
            if not self.sr.lvActivator.deactivateAll():
                util.SMlog("Failed to deactivate LVs back (%s)" % self.uuid)


    def detach(self, sr_uuid, vdi_uuid):
        util.SMlog("LVHDVDI.detach for %s" % self.uuid)
        self._loadThis()
        already_deflated = (self.utilisation < \
                lvhdutil.calcSizeVHDLV(self.size))
        needDeflate = True
        if self.vdi_type == lvhdutil.VDI_TYPE_RAW or already_deflated:
            needDeflate = False
        elif not self.sr.thinpr:
            needDeflate = False
            # except for snapshots, which are always deflated
            vdi_ref = self.sr.srcmd.params['vdi_ref']
            snap = self.session.xenapi.VDI.get_is_a_snapshot(vdi_ref)
            if snap:
                needDeflate = True

        if needDeflate:
            try:
                self._prepareThin(False)
            except:
                util.logException("_prepareThin")
                raise xs_errors.XenError('VDIUnavailable', opterr='deflate')

        try:
            self._detach()
        finally:
            if not self.sr.lvActivator.deactivateAll():
                raise xs_errors.XenError("SMGeneral", opterr="deactivation")

    # We only support offline resize
    @locking("VDIResize")
    def resize(self, sr_uuid, vdi_uuid, size):
        util.SMlog("LVHDVDI.resize for %s" % self.uuid)
        if not self.sr.isMaster:
            raise xs_errors.XenError('LVMMaster')

        if size / 1024 / 1024 > self.MAX_VDI_SIZE_MB:
            raise xs_errors.XenError('VDISize',
                    opterr="VDI size cannot exceed %d MB" % \
                            self.MAX_VDI_SIZE_MB)

        self._loadThis()
        if self.hidden:
            raise xs_errors.XenError('VDIUnavailable', opterr='hidden VDI')
        
        if size < self.size:
            util.SMlog('vdi_resize: shrinking not supported: ' + \
                    '(current size: %d, new size: %d)' % (self.size, size))
            raise xs_errors.XenError('VDISize', opterr='shrinking not allowed')

        if size == self.size:
            return VDI.VDI.get_params(self)

        size = util.roundup(self.VHD_SIZE_INC, size)
        if self.vdi_type == lvhdutil.VDI_TYPE_RAW:
            lvSizeOld = self.size
            lvSizeNew = util.roundup(lvutil.LVM_SIZE_INCREMENT, size)
        else:
            lvSizeOld = self.utilisation
            lvSizeNew = lvhdutil.calcSizeVHDLV(size)
            if self.sr.thinpr:
                # VDI is currently deflated, so keep it deflated
                lvSizeNew = lvSizeOld
        assert(lvSizeNew >= lvSizeOld)
        spaceNeeded = lvSizeNew - lvSizeOld
        self.sr._ensureSpaceAvailable(spaceNeeded)

        oldSize = self.size
        if self.vdi_type == lvhdutil.VDI_TYPE_RAW:
            self.sr.lvmCache.setSize(self.lvname, lvSizeNew)
            self.size = self.sr.lvmCache.getSize(self.lvname)
            self.utilisation = self.size
        else:
            if lvSizeNew != lvSizeOld:
                lvhdutil.inflate(self.sr.journaler, self.sr.uuid, self.uuid,
                        lvSizeNew)
            vhdutil.setSizeVirtFast(self.path, size)
            self.size = vhdutil.getSizeVirt(self.path)
            self.utilisation = self.sr.lvmCache.getSize(self.lvname)

        vdi_ref = self.sr.srcmd.params['vdi_ref']
        self.session.xenapi.VDI.set_virtual_size(vdi_ref, str(self.size))
        self.session.xenapi.VDI.set_physical_utilisation(vdi_ref,
                str(self.utilisation))
        self.sr._updateStats(self.sr.uuid, self.size - oldSize)
        return VDI.VDI.get_params(self)

    @locking("VDISnapshot")
    def snapshot(self, sr_uuid, vdi_uuid):
        # logically, "snapshot" should mean SNAPSHOT_SINGLE and "clone" should 
        # mean "SNAPSHOT_DOUBLE", but in practice we have to do SNAPSHOT_DOUBLE 
        # in both cases, unless driver_params overrides it
        snapType = self.SNAPSHOT_DOUBLE
        if self.sr.srcmd.params['driver_params'].get("type") and \
                self.sr.srcmd.params['driver_params']["type"] == "single":
            snapType = self.SNAPSHOT_SINGLE
        return self._snapshot(snapType)

    @locking("VDIClone")
    def clone(self, sr_uuid, vdi_uuid):
        return self._snapshot(self.SNAPSHOT_DOUBLE)

    def _attach(self):
        self._chainSetActive(True, True, True)
        if not util.pathexists(self.path):
            raise xs_errors.XenError('VDIUnavailable', \
                    opterr='Could not find: %s' % self.path)

        if self.sr.srcmd.params.has_key("vdi_ref"):
            vdi_ref = self.sr.srcmd.params['vdi_ref']
            scsiutil.update_XS_SCSIdata(self.session, vdi_ref, self.uuid, \
                    scsiutil.gen_synthetic_page_data(self.uuid))
            try:
                self.session.xenapi.VDI.remove_from_xenstore_data(vdi_ref, \
                        "storage-type")
                self.session.xenapi.VDI.remove_from_xenstore_data(vdi_ref, \
                        "vdi-type")
                self.session.xenapi.VDI.add_to_xenstore_data(vdi_ref, \
                        "storage-type", "lvm")
                self.session.xenapi.VDI.add_to_xenstore_data(vdi_ref, \
                        "vdi-type", self.vdi_type)
            except XenAPI.Failure:
                util.logException("LVHDVDI.attach")

        self.attached = True
        self.sr.lvActivator.persist()
        return VDI.VDI.attach(self, self.sr.uuid, self.uuid)

    def _detach(self):
        self._chainSetActive(False, True)
        self.attached = False

    def _snapshot(self, snapType):
        util.SMlog("LVHDVDI._snapshot for %s (type %s)" % (self.uuid, snapType))

        if not self.sr.isMaster:
            raise xs_errors.XenError('LVMMaster')
        if self.sr.legacyMode:
            raise xs_errors.XenError('Unimplemented', opterr='In legacy mode')

        self._loadThis()
        if self.hidden:
            raise xs_errors.XenError('VDISnapshot', opterr='hidden VDI')

        self.sm_config = self.session.xenapi.VDI.get_sm_config( \
                self.sr.srcmd.params['vdi_ref'])
        if self.sm_config.has_key("type") and self.sm_config['type']=='raw':
            if not util.fistpoint.is_active("testsm_clone_allow_raw"):
                raise xs_errors.XenError('Unimplemented', \
                        opterr='Raw VDI, snapshot or clone not permitted')
       
        # we must activate the entire VHD chain because the real parent could 
        # theoretically be anywhere in the chain if all VHDs under it are empty
        self._chainSetActive(True, False)
        if not util.pathexists(self.path):
            raise xs_errors.XenError('VDIUnavailable', \
                    opterr='VDI unavailable: %s' % (self.path))

        if self.vdi_type == lvhdutil.VDI_TYPE_VHD:
            depth = vhdutil.getDepth(self.path)
            if depth == -1:
                raise xs_errors.XenError('VDIUnavailable', \
                        opterr='failed to get VHD depth')
            elif depth >= vhdutil.MAX_CHAIN_SIZE:
                raise xs_errors.XenError('SnapshotChainTooLong')

        self.issnap = self.session.xenapi.VDI.get_is_a_snapshot(self.sr.srcmd.params['vdi_ref'])

        fullpr = lvhdutil.calcSizeVHDLV(self.size)
        thinpr = util.roundup(lvutil.LVM_SIZE_INCREMENT, \
                vhdutil.calcOverheadEmpty(lvhdutil.MSIZE))
        lvSizeOrig = thinpr
        lvSizeClon = thinpr

        self.activeVBDs = []
        if self.sr.cmd == "vdi_snapshot":
            self.activeVBDs = util.get_attached_VBDs(self.session, self.uuid)
            if len(self.activeVBDs):
                lvSizeOrig = fullpr
        if not self.sr.thinpr:
            if not self.issnap:
                lvSizeOrig = fullpr
            if self.sr.cmd != "vdi_snapshot":
                lvSizeClon = fullpr

        if snapType == self.SNAPSHOT_SINGLE:
            lvSizeClon = 0

        # the space required must include 2 journal LVs: a clone journal and an 
        # inflate journal (for the failure handling
        size_req = lvSizeOrig + lvSizeClon + 2 * self.sr.journaler.LV_SIZE
        lvSizeBase = self.size
        if self.vdi_type == lvhdutil.VDI_TYPE_VHD:
            lvSizeBase = util.roundup(lvutil.LVM_SIZE_INCREMENT,
                    vhdutil.getSizePhys(self.path))
            size_req -= (self.utilisation - lvSizeBase)
        self.sr._ensureSpaceAvailable(size_req)

        baseUuid = util.gen_uuid()
        origUuid = self.uuid
        clonUuid = ""
        if snapType == self.SNAPSHOT_DOUBLE:
            clonUuid = util.gen_uuid()
        jval = "%s_%s" % (baseUuid, clonUuid)
        self.sr.journaler.create(self.JRN_CLONE, origUuid, jval)
        util.fistpoint.activate("LVHDRT_clone_vdi_after_create_journal",self.sr.uuid)

        try:
            # self becomes the "base vdi"
            origOldLV = self.lvname
            baseLV = lvhdutil.LV_PREFIX[self.vdi_type] + baseUuid
            self.sr.lvmCache.rename(self.lvname, baseLV)
            self.sr.lvActivator.replace(self.uuid, baseUuid, baseLV, False)
            self.uuid = baseUuid
            self.lvname = baseLV
            self.path = os.path.join(self.sr.path, baseLV)
            self.label = "base copy"
            self.read_only = True
            self.location = self.uuid
            self.managed = False

            # shrink the base copy to the minimum - we do it before creating 
            # the snapshot volumes to avoid requiring double the space
            if self.vdi_type == lvhdutil.VDI_TYPE_VHD:
                lvhdutil.deflate(self.sr.lvmCache, self.lvname, lvSizeBase)
                self.utilisation = lvSizeBase
            util.fistpoint.activate("LVHDRT_clone_vdi_after_shrink_parent", self.sr.uuid)

            snapVDI = self._createSnap(origUuid, lvSizeOrig, False)
            util.fistpoint.activate("LVHDRT_clone_vdi_after_first_snap", self.sr.uuid)
            snapVDI2 = None
            if snapType == self.SNAPSHOT_DOUBLE:
                snapVDI2 = self._createSnap(clonUuid, lvSizeClon, True)
            util.fistpoint.activate("LVHDRT_clone_vdi_after_second_snap", self.sr.uuid)

            # note: it is important to mark the parent hidden only AFTER the 
            # new VHD children have been created, which are referencing it; 
            # otherwise we would introduce a race with GC that could reclaim 
            # the parent before we snapshot it 
            if self.vdi_type == lvhdutil.VDI_TYPE_RAW:
                self.sr.lvmCache.setHidden(self.lvname)
            else:
                vhdutil.setHidden(self.path)
            util.fistpoint.activate("LVHDRT_clone_vdi_after_parent_hidden", self.sr.uuid)

            # set the base copy to ReadOnly
            self.sr.lvmCache.setReadonly(self.lvname, True)
            util.fistpoint.activate("LVHDRT_clone_vdi_after_parent_ro", self.sr.uuid)

            if len(self.activeVBDs):
                self.sr._updateSlavesOnClone(self.activeVBDs,
                        origOldLV, snapVDI.lvname, self.uuid, self.lvname)

        except (util.SMException, XenAPI.Failure), e:
            util.logException("LVHDVDI._snapshot")
            self._failClone(origUuid, jval, str(e))
        util.fistpoint.activate("LVHDRT_clone_vdi_before_remove_journal",self.sr.uuid)
        self.sr.journaler.remove(self.JRN_CLONE, origUuid)

        return self._finishSnapshot(snapVDI, snapVDI2)


    def _createSnap(self, snapUuid, snapSizeLV, isNew):
        """Snapshot self and return the snapshot VDI object"""
        snapLV = lvhdutil.LV_PREFIX[lvhdutil.VDI_TYPE_VHD] + snapUuid
        snapPath = os.path.join(self.sr.path, snapLV)
        self.sr.lvmCache.create(snapLV, long(snapSizeLV))
        util.fistpoint.activate("LVHDRT_clone_vdi_after_lvcreate", self.sr.uuid)
        if isNew:
            RefCounter.set(snapUuid, 1, 0, lvhdutil.NS_PREFIX_LVM + self.sr.uuid)
        self.sr.lvActivator.add(snapUuid, snapLV, False)
        parentRaw = (self.vdi_type == lvhdutil.VDI_TYPE_RAW)
        vhdutil.snapshot(snapPath, self.path, parentRaw, lvhdutil.MSIZE_MB)
        snapParent = vhdutil.getParent(snapPath, lvhdutil.extractUuid)

        snapVDI = VDI.VDI(self.sr, snapUuid)
        snapVDI.read_only = False
        snapVDI.location = snapUuid
        snapVDI.size = self.size
        snapVDI.utilisation = snapSizeLV
        snapVDI.sm_config = dict()
        for key, val in self.sm_config.iteritems():
            if key not in ["type", "vdi_type", "vhd-parent"]:
                snapVDI.sm_config[key] = val
        snapVDI.sm_config["vdi_type"] = lvhdutil.VDI_TYPE_VHD
        snapVDI.sm_config["vhd-parent"] = snapParent
        snapVDI.lvname = snapLV
        return snapVDI

    def _finishSnapshot(self, snapVDI, snapVDI2):
        self.sr._updateStats(self.sr.uuid, self.size)
        basePresent = True

        # Verify parent locator field of both children and delete basePath if 
        # unused
        snapParent = snapVDI.sm_config["vhd-parent"]
        snap2Parent = ""
        if snapVDI2:
            snap2Parent = snapVDI2.sm_config["vhd-parent"]
        if snapParent != self.uuid and \
                (not snapVDI2 or snap2Parent != self.uuid):
            util.SMlog("%s != %s != %s => deleting unused base %s" % \
                    (snapParent, self.uuid, snap2Parent, self.lvname))
            self.sr.lvmCache.remove(self.lvname)
            self.sr.lvActivator.remove(self.uuid, False)
            basePresent = False
        else:
            # assign the _binary_ refcount of the original VDI to the new base 
            # VDI (but as the normal refcount, since binary refcounts are only 
            # for leaf nodes). The normal refcount of the child is not 
            # transferred to to the base VDI because normal refcounts are 
            # incremented and decremented individually, and not based on the 
            # VHD chain (i.e., the child's normal refcount will be decremented 
            # independently of its parent situation). Add 1 for this clone op.
            # Note that we do not need to do protect the refcount operations 
            # below with per-VDI locking like we do in lvutil because at this 
            # point we have exclusive access to the VDIs involved. Other SM 
            # operations are serialized by the Agent or with the SR lock, and 
            # any coalesce activations are serialized with the SR lock.  (The 
            # coalesce activates the coalesced VDI pair in the beginning, which 
            # cannot affect the VDIs here because they cannot  possibly be 
            # involved in coalescing at this point, and at the relinkSkip step 
            # that activates the children, which takes the SR lock.)
            ns = lvhdutil.NS_PREFIX_LVM + self.sr.uuid
            (cnt, bcnt) = RefCounter.check(snapVDI.uuid, ns)
            RefCounter.set(self.uuid, bcnt + 1, 0, ns)

        # Introduce any new VDI records & update the existing one
        if snapVDI2:
            vdiRef = snapVDI2._db_introduce()
            util.SMlog("vdi_clone: introduced 2nd snap VDI: %s (%s)" % \
                       (vdiRef, snapVDI2.uuid))

        if basePresent:
            vdiRef = self._db_introduce()
            util.SMlog("vdi_clone: introduced base VDI: %s (%s)" % \
                    (vdiRef, self.uuid))

        # Update the original record
        vdi_ref = self.sr.srcmd.params['vdi_ref']
        self.session.xenapi.VDI.set_sm_config(vdi_ref, snapVDI.sm_config)
        self.session.xenapi.VDI.set_physical_utilisation(vdi_ref, \
                str(snapVDI.utilisation))

        # Return the info on the new snap VDI
        snap = snapVDI2
        if not snap:
            snap = self
            if not basePresent:
                # a single-snapshot of an empty VDI will be a noop, resulting 
                # in no new VDIs, so return the existing one. The GC wouldn't 
                # normally try to single-snapshot an empty VHD of course, but 
                # if an external snapshot operation manages to sneak in right 
                # before a snapshot-coalesce phase, we would get here
                snap = snapVDI
        return snap.get_params()

    def _initFromVDIInfo(self, vdiInfo):
        self.vdi_type    = vdiInfo.vdiType
        self.lvname      = vdiInfo.lvName
        self.size        = vdiInfo.sizeVirt
        self.utilisation = vdiInfo.sizeLV
        self.hidden      = vdiInfo.hidden
        if self.hidden:
            self.managed = False
        self.active      = vdiInfo.lvActive
        self.readonly    = vdiInfo.lvReadonly
        self.parent      = vdiInfo.parentUuid
        self.path        = os.path.join(self.sr.path, self.lvname)
        if hasattr(self, "sm_config_override"):
            self.sm_config_override["vdi_type"] = self.vdi_type
        else:
            self.sm_config_override = {'vdi_type': self.vdi_type}
        self.loaded = True

    def _initFromLVInfo(self, lvInfo):
        self.vdi_type    = lvInfo.vdiType
        self.lvname      = lvInfo.name
        self.size        = lvInfo.size
        self.utilisation = lvInfo.size
        self.hidden      = lvInfo.hidden
        self.active      = lvInfo.active
        self.readonly    = lvInfo.readonly
        self.parent      = ''
        self.path        = os.path.join(self.sr.path, self.lvname)
        if hasattr(self, "sm_config_override"):
            self.sm_config_override["vdi_type"] = self.vdi_type
        else:
            self.sm_config_override = {'vdi_type': self.vdi_type}
        if self.vdi_type == lvhdutil.VDI_TYPE_RAW:
            self.loaded = True

    def _initFromVHDInfo(self, vhdInfo):
        self.size   = vhdInfo.sizeVirt
        self.parent = vhdInfo.parentUuid
        self.hidden = vhdInfo.hidden
        self.loaded = True

    def _determineType(self):
        """Determine whether this is a raw or a VHD VDI"""
        if self.sr.srcmd.params.has_key("vdi_ref"):
            vdi_ref = self.sr.srcmd.params["vdi_ref"]
            sm_config = self.session.xenapi.VDI.get_sm_config(vdi_ref)
            if sm_config.get("vdi_type"):
                self.vdi_type = sm_config["vdi_type"]
                prefix = lvhdutil.LV_PREFIX[self.vdi_type]
                self.lvname = "%s%s" % (prefix, self.uuid)
                self.path = os.path.join(self.sr.path, self.lvname)
                self.sm_config_override = sm_config
                return True

        # LVM commands can be costly, so check the file directly first in case 
        # the LV is active
        found = False
        for t in lvhdutil.VDI_TYPES:
            lvname = "%s%s" % (lvhdutil.LV_PREFIX[t], self.uuid)
            path = os.path.join(self.sr.path, lvname)
            if util.pathexists(path):
                if found:
                    raise xs_errors.XenError('VDILoad',
                            opterr="multiple VDI's: uuid %s" % self.uuid)
                found = True
                self.vdi_type = t
                self.lvname   = lvname
                self.path     = path
        if found:
            return True

        # now list all LV's
        if not lvutil._checkVG(self.sr.vgname):
            # when doing attach_from_config, the VG won't be there yet
            return False

        lvs = lvhdutil.getLVInfo(self.sr.lvmCache)
        if lvs.get(self.uuid):
            self._initFromLVInfo(lvs[self.uuid])
            return True
        return False

    def _loadThis(self):
        """Load VDI info for this VDI and activate the LV if it's VHD. We
        don't do it in VDI.load() because not all VDI operations need it."""
        if self.loaded:
            if self.vdi_type == lvhdutil.VDI_TYPE_VHD:
                self.sr.lvActivator.activate(self.uuid, self.lvname, False)
            return
        try:
            lvs = lvhdutil.getLVInfo(self.sr.lvmCache, self.lvname)
        except util.CommandException:
            raise xs_errors.XenError('VDIUnavailable', opterr='LV scan error')
        if not lvs.get(self.uuid):
            raise xs_errors.XenError('VDIUnavailable', opterr='LV not found')
        self._initFromLVInfo(lvs[self.uuid])
        if self.vdi_type == lvhdutil.VDI_TYPE_VHD:
            self.sr.lvActivator.activate(self.uuid, self.lvname, False)
            vhdInfo = vhdutil.getVHDInfo(self.path, lvhdutil.extractUuid, False)
            if not vhdInfo:
                raise xs_errors.XenError('VDIUnavailable', \
                        opterr='getVHDInfo failed')
            self._initFromVHDInfo(vhdInfo)
        self.loaded = True

    def _chainSetActive(self, active, binary, persistent = False):
        if binary:
            (count, bcount) = RefCounter.check(self.uuid, 
                lvhdutil.NS_PREFIX_LVM + self.sr.uuid)
            if (active and bcount > 0) or (not active and bcount == 0):
                return # this is a redundant activation/deactivation call

        vdiList = {self.uuid: self.lvname}
        if self.vdi_type == lvhdutil.VDI_TYPE_VHD:
            vdiList = vhdutil.getParentChain(self.lvname,
                    lvhdutil.extractUuid, self.sr.vgname)
        for uuid, lvName in vdiList.iteritems():
            binaryParam = binary
            if uuid != self.uuid:
                binaryParam = False # binary param only applies to leaf nodes
            if active:
                self.sr.lvActivator.activate(uuid, lvName, binaryParam,
                        persistent)
            else:
                # just add the LVs for deactivation in the final (cleanup) 
                # step. The LVs must not have been activated during the current 
                # operation
                self.sr.lvActivator.add(uuid, lvName, binaryParam)

    def _failClone(self, uuid, jval, msg):
        self.sr._handleInterruptedCloneOp(uuid, jval, True)
        self.sr.journaler.remove(self.JRN_CLONE, uuid)
        raise xs_errors.XenError('VDIClone', opterr=msg)

    def _markHidden(self):
        if self.vdi_type == lvhdutil.VDI_TYPE_RAW:
            self.sr.lvmCache.setHidden(self.lvname)
        else:
            vhdutil.setHidden(self.path)
        self.hidden = 1

    def _prepareThin(self, attach):
        origUtilisation = self.sr.lvmCache.getSize(self.lvname)
        if self.sr.isMaster:
            # the master can prepare the VDI locally
            if attach:
                lvhdutil.attachThin(self.sr.journaler, self.sr.uuid, self.uuid)
            else:
                lvhdutil.detachThin(self.session, self.sr.lvmCache,
                        self.sr.uuid, self.uuid)
        else:
            fn = "attach"
            if not attach:
                fn = "detach"
            pools = self.session.xenapi.pool.get_all()
            master = self.session.xenapi.pool.get_master(pools[0])
            text = self.session.xenapi.host.call_plugin( \
                    master, self.sr.THIN_PLUGIN, fn, \
                    {"srUuid": self.sr.uuid, "vdiUuid": self.uuid})
            util.SMlog("call-plugin returned: '%s'" % text)

        self.utilisation = self.sr.lvmCache.getSize(self.lvname)
        if origUtilisation != self.utilisation:
            vdi_ref = self.sr.srcmd.params['vdi_ref']
            self.session.xenapi.VDI.set_physical_utilisation(vdi_ref,
                    str(self.utilisation))
            stats = lvutil._getVGstats(self.sr.vgname)
            sr_utilisation = stats['physical_utilisation']
            self.session.xenapi.SR.set_physical_utilisation(self.sr.sr_ref,
                    str(sr_utilisation))



try:
    if __name__ == '__main__':
        SRCommand.run(LVHDSR, DRIVER_INFO)
    else:
        SR.registerSR(LVHDSR)
except Exception:
    util.logException("LVHD")
    raise

