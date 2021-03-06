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
# VDI: Base class for virtual disk instances
#

import SR
import xml.dom.minidom
import xmlrpclib
import xs_errors
import util

def VDIMetadataSize(type, virtualsize):
    size = 0
    if type == 'vhd':
        size_mb = virtualsize / (1024 * 1024)
        #Footer + footer copy + header + possible CoW parent locator fields
        size = 3 * 1024

        # BAT 4 Bytes per block segment
        size += (size_mb / 2) * 4
        size = util.roundup(512, size)

        # BATMAP 1 bit per block segment
        size += (size_mb / 2) / 8
        size = util.roundup(4096, size)

        # Segment bitmaps + Page align offsets
        size += (size_mb / 2) * 4096
    elif type == 'qcow':
        # Header + extended header
        size = 46 + 17
        size = util.roundup(512, size)

        # L1 table
        size += (size_mb / 2) * 8
        size = util.roundup(4096, size)

        # L2 tables
        size += (size_mb / 2) * 4096
    return size

class VDI(object):
    """Virtual Disk Instance descriptor.

    Attributes:
      uuid: string, globally unique VDI identifier conforming to OSF DEC 1.1
      label: string, user-generated tag string for identifyng the VDI
      description: string, longer user generated description string
      size: int, virtual size in bytes of this VDI
      utilisation: int, actual size in Bytes of data on disk that is 
        utilised. For non-sparse disks, utilisation == size
      vdi_type: string, disk type, e.g. raw file, partition
      parent: VDI object, parent backing VDI if this disk is a 
     CoW instance
      shareable: boolean, does this disk support multiple writer instances?
        e.g. shared OCFS disk
      attached: boolean, whether VDI is attached
      read_only: boolean, whether disk is read-only.
    """
    def __init__(self, sr, uuid):
        self.sr = sr
	# Don't set either the UUID or location to None- no good can
	# ever come of this.
	if uuid <> None:
		self.uuid = uuid
		self.location = uuid
        # deliberately not initialised self.sm_config so that it is
        # ommitted from the XML output

        self.label = ''
        self.description = ''
        self.vbds = []
        self.size = 0
        self.utilisation = 0
        self.vdi_type = ''
        self.has_child = 0
        self.parent = None
        self.shareable = False
        self.attached = False
        self.status = 0
        self.read_only = False
        self.xenstore_keys = ''
        self.deleted = False
        self.session = sr.session
        self.managed = True
        self.sm_config_override = {}

        self.load(uuid)

    def create(self, sr_uuid, vdi_uuid, size):
        """Create a VDI of size <Size> MB on the given SR. 

        This operation IS NOT idempotent and will fail if the UUID
        already exists or if there is insufficient space. The vdi must
        be explicitly attached via the attach() command following
        creation. The actual disk size created may be larger than the
        requested size if the substrate requires a size in multiples
        of a certain extent size. The SR must be queried for the exact
        size.
        """
        raise xs_errors.XenError('Unimplemented')

    def update(self, sr_uuid, vdi_uuid):
        """Query and update the configuration of a particular VDI.

        Given an SR and VDI UUID, this operation returns summary statistics
        on the named VDI. Note the XenAPI VDI object will exist when
        this call is made.
        """
        raise xs_errors.XenError('Unimplemented')

    def introduce(self, sr_uuid, vdi_uuid):
        """Explicitly introduce a particular VDI.

        Given an SR and VDI UUID and a disk location (passed in via the <conf>
        XML), this operation verifies the existence of the underylying disk
        object and then creates the XenAPI VDI object.
        """
        raise xs_errors.XenError('Unimplemented')

    def delete(self, sr_uuid, vdi_uuid):
        """Delete this VDI.

        This operation IS idempotent and should succeed if the VDI
        exists and can be deleted or if the VDI does not exist. It is
        the responsibility of the higher-level management tool to
        ensure that the detach() operation has been explicitly called
        prior to deletion, otherwise the delete() will fail if the
        disk is still attached.
        """
        raise xs_errors.XenError('Unimplemented')

    def attach(self, sr_uuid, vdi_uuid):
        """Initiate local access to the VDI. Initialises any device
        state required to access the VDI.

        This operation IS idempotent and should succeed if the VDI can be
        attached or if the VDI is already attached.

        Returns:
          string, local device path.
        """
        return xmlrpclib.dumps((self.path,), "", True)

    def detach(self, sr_uuid, vdi_uuid):
        """Remove local access to the VDI. Destroys any device 
        state initialised via the vdi.attach() command.

        This operation is idempotent.
        """
        raise xs_errors.XenError('Unimplemented')

    def clone(self, sr_uuid, vdi_uuid):
        """Create a mutable instance of the referenced VDI.

        This operation is not idempotent and will fail if the UUID
        already exists or if there is insufficient space. The SRC VDI
        must be in a detached state and deactivated. Upon successful
        creation of the clone, the clone VDI must be explicitly
        attached via vdi.attach(). If the driver does not support
        cloning this operation should raise SRUnsupportedOperation.

        Arguments:
        Raises:
          SRUnsupportedOperation
        """
        raise xs_errors.XenError('Unimplemented')

    def snapshot(self, sr_uuid, vdi_uuid):
        """Save an immutable copy of the referenced VDI.

        This operation IS NOT idempotent and will fail if the UUID
        already exists or if there is insufficient space. The vdi must
        be explicitly attached via the vdi_attach() command following
        creation. If the driver does not support snapshotting this
        operation should raise SRUnsupportedOperation

        Arguments:
        Raises:
          SRUnsupportedOperation
        """
        raise xs_errors.XenError('Unimplemented')

    def resize(self, sr_uuid, vdi_uuid, size):
        """Resize the given VDI to size <size> MB. Size can
        be any valid disk size greater than [or smaller than]
        the current value.

        This operation IS idempotent and should succeed if the VDI can
        be resized to the specified value or if the VDI is already the
        specified size. The actual disk size created may be larger
        than the requested size if the substrate requires a size in
        multiples of a certain extent size. The SR must be queried for
        the exact size. This operation does not modify the contents on
        the disk such as the filesystem.  Responsibility for resizing
        the FS is left to the VM administrator. [Reducing the size of
        the disk is a very dangerous operation and should be conducted
        very carefully.] Disk contents should always be backed up in
        advance.
        """
        raise xs_errors.XenError('Unimplemented')

    def resize_online(self, sr_uuid, vdi_uuid, size):
        """Resize the given VDI which may have active VBDs, which have
        been paused for the duration of this call."""
        raise xs_errors.XenError('Unimplemented')

    def generate_config(self, sr_uuid, vdi_uuid):
        """Generate the XML config required to activate a VDI for use
        when XAPI is not running. Activation is handled by the
        vdi_attach_from_config() SMAPI call.
        """
        raise xs_errors.XenError('Unimplemented')

    def attach_from_config(self, sr_uuid, vdi_uuid):
        """Activate a VDI based on the config passed in on the CLI. For
        use when XAPI is not running. The config is generated by the
        Activation is handled by the vdi_generate_config() SMAPI call.
        """
        raise xs_errors.XenError('Unimplemented')

    def activate(self, sr_uuid, vdi_uuid):
        """To be fleshed out soon..."""
        raise xs_errors.XenError('Unimplemented')

    def deactivate(self, sr_uuid, vdi_uuid):
        """To be fleshed out soon..."""
        raise xs_errors.XenError('Unimplemented')

    def get_params(self):
        """
        Returns:
          XMLRPC response containing a single struct with fields
          'location' and 'uuid'
        """
        struct = { 'location': self.location,
                   'uuid': self.uuid }
        return xmlrpclib.dumps((struct,), "", True)

    def load(self, vdi_uuid):
        """Post-init hook"""
        pass

    def _db_introduce(self):
        uuid = util.default(self, "uuid", lambda: util.gen_uuid())
        sm_config = util.default(self, "sm_config", lambda: {})
        vdi = self.sr.session.xenapi.VDI.db_introduce(uuid, self.label, self.description, self.sr.sr_ref, "user", self.shareable, self.read_only, {}, self.location, {}, {})
        self.sr.session.xenapi.VDI.set_sm_config(vdi, sm_config)
        self.sr.session.xenapi.VDI.set_managed(vdi, self.managed)
        self.sr.session.xenapi.VDI.set_virtual_size(vdi, str(self.size))
        self.sr.session.xenapi.VDI.set_physical_utilisation(vdi, str(self.utilisation))
        return vdi

    def _db_forget(self):
        self.sr.forget_vdi(self.uuid)

    def _db_update(self):
        vdi = self.sr.session.xenapi.VDI.get_by_uuid(self.uuid)
        self.sr.session.xenapi.VDI.set_virtual_size(vdi, str(self.size))
        self.sr.session.xenapi.VDI.set_physical_utilisation(vdi, str(self.utilisation))
        self.sr.session.xenapi.VDI.set_read_only(vdi, self.read_only)
        sm_config = util.default(self, "sm_config", lambda: {})
        self.sr.session.xenapi.VDI.set_sm_config(vdi, sm_config)
        
    def in_sync_with_xenapi_record(self, x):
        """Returns true if this VDI is in sync with the supplied XenAPI record"""
        if self.location <> x['location']:
            util.SMlog("location %s <> %s" % (self.location, x['location']))
            return False
        if self.read_only <> x['read_only']:
            util.SMlog("read_only %s <> %s" % (self.read_only, x['read_only']))
            return False
        if str(self.size) <> x['virtual_size']:
            util.SMlog("virtual_size %s <> %s" % (self.size, x['virtual_size']))
            return False
        if str(self.utilisation) <> x['physical_utilisation']:
            util.SMlog("utilisation %s <> %s" % (self.utilisation, x['physical_utilisation']))
            return False
        sm_config = util.default(self, "sm_config", lambda: {})
        if set(sm_config.keys()) <> set(x['sm_config'].keys()):
            util.SMlog("sm_config %s <> %s" % (repr(sm_config), repr(x['sm_config'])))
            return False
        for k in sm_config.keys():
            if sm_config[k] <> x['sm_config'][k]:
                util.SMlog("sm_config %s <> %s" % (repr(sm_config), repr(x['sm_config'])))
                return False
        return True
        
