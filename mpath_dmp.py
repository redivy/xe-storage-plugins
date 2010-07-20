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

import util
import xs_errors
import statvfs
import stat
import iscsilib
import mpath_cli
import os
import glob
import time
import scsiutil

mpath_disable_file = "/etc/multipath-disabled.conf"
mpath_enable_file = "/etc/multipath-enabled.conf"
mpath_file = "/etc/multipath.conf"
iscsi_mpath_file = "/etc/iscsi/iscsid-mpath.conf"
iscsi_default_file = "/etc/iscsi/iscsid-default.conf"
iscsi_file = "/etc/iscsi/iscsid.conf"
hba_script = "/opt/xensource/sm/mpathHBA"

DMPBIN = "/sbin/multipath"
DEVMAPPERPATH = "/dev/mapper"
DEVBYIDPATH = "/dev/disk/by-id"
MP_INUSEDIR = "/dev/disk/mpInuse"

def _is_mpath_daemon_running():
    cmd = ["/sbin/pidof", "-s", "/sbin/multipathd"]
    (rc,stdout,stderr) = util.doexec(cmd)
    return (rc==0)

def activate_MPdev(sid, dst):
    if not os.path.exists(MP_INUSEDIR):
        os.mkdir(MP_INUSEDIR)
    path = os.path.join(MP_INUSEDIR, sid)
    cmd = ['ln', '-sf', dst, path]
    util.pread2(cmd)

def deactivate_MPdev(sid):
    path = os.path.join(MP_INUSEDIR, sid)
    if os.path.exists(path):
        os.unlink(path)
        
def reset(sid,explicit_unmap=False):
# If mpath has been turned on since the sr/vdi was attached, we
# might be trying to unmap it before the daemon has been started
# This is unnecessary (and will fail) so just return.
    deactivate_MPdev(sid)
    if not _is_mpath_daemon_running():
        util.SMlog("Warning: Trying to unmap mpath device when multipathd not running")
        return

# If the multipath daemon is running, but we were initially plugged
# with multipathing set to no, there may be no map for us in the multipath
# tables. In that case, list_paths will return [], but remove_map might
# throw an exception. Catch it and ignore it.
    if explicit_unmap:
        util.SMlog("Explicit unmap")
        devices = mpath_cli.list_paths(sid)

        try:
            mpath_cli.remove_map(sid)
        except:
            util.SMlog("Warning: Removing the path failed")
            pass
        
        for device in devices:
            mpath_cli.remove_path(device)
    else:
        mpath_cli.ensure_map_gone(sid)

    path = "/dev/mapper/%s" % sid
    
    if not util.wait_for_nopath(path, 10):
        util.SMlog("MPATH: WARNING - path did not disappear [%s]" % path)
    else:
        util.SMlog("MPATH: path disappeared [%s]" % path)

# expecting e.g. ["/dev/sda","/dev/sdb"] or ["/dev/disk/by-scsibus/...whatever" (links to the real devices)]
def __map_explicit(devices):
    for device in devices:
        realpath = os.path.realpath(device)
        base = os.path.basename(realpath)
        util.SMlog("Adding mpath path '%s'" % base)
        try:
            mpath_cli.add_path(base)
        except:
            util.SMlog("WARNING: exception raised while attempting to add path %s" % base)

def map_by_scsibus(sid,npaths=0):
    # Synchronously creates/refreshs the MP map for a single SCSIid.
    # Gathers the device vector from /dev/disk/by-scsibus - we expect
    # there to be 'npaths' paths

    util.SMlog("map_by_scsibus: sid=%s" % sid)

    devices = []

    # Wait for up to 60 seconds for n devices to appear
    for attempt in range(0,60):
        devices = scsiutil._genReverseSCSIidmap(sid)

        # If we've got the right number of paths, or we don't know
        # how many devices there ought to be, tell multipathd about
        # the paths, and return.
        if(len(devices)>=npaths or npaths==0):
            __map_explicit(devices)
            return

        time.sleep(1)

    __map_explicit(devices)
    
def refresh(sid,npaths):
    # Refresh the multipath status
    if len(sid):
        map_by_scsibus(sid,npaths)
    else:
        raise xs_errors.XenError('MPath not written yet')
    path = os.path.join(DEVMAPPERPATH, sid)
    activate_MPdev(sid, path)

def activate():
    util.SMlog("MPATH: dm-multipath activate called")
    # Adjust any HBAs on the host
    if os.path.realpath(mpath_file) != mpath_enable_file:
        cmd = [hba_script, "enable"]
        util.SMlog(util.pread2(cmd))
    cmd = ['ln', '-sf', mpath_enable_file, mpath_file]
    util.pread2(cmd)
    cmd = ['ln', '-sf', iscsi_mpath_file, iscsi_file]
    util.pread2(cmd)

    # If we've got no active sessions, and the deamon is already running,
    # we're ok to restart the daemon
    if iscsilib.is_iscsi_daemon_running():
        if not iscsilib._checkAnyTGT():
            iscsilib.restart_daemon()
        
    if not _is_mpath_daemon_running():
        cmd = ["/etc/init.d/multipathd", "start"]
        util.pread2(cmd)

    for i in range(0,120):
        if mpath_cli.is_working():
            util.SMlog("MPATH: dm-multipath activated.")
            return
        time.sleep(1)

    util.SMlog("Failed to communicate with the multipath daemon!")
    raise xs_errors.XenError('MultipathdCommsFailure')    

def deactivate():
    util.SMlog("MPATH: dm-multipath deactivate called")
    # Adjust any HBAs on the host
    if os.path.realpath(mpath_file) != mpath_disable_file:
        cmd = [hba_script, "disable"]
        util.SMlog(util.pread2(cmd))
    cmd = ['ln', '-sf', mpath_disable_file, mpath_file]
    util.pread2(cmd)
    cmd = ['ln', '-sf', iscsi_default_file, iscsi_file]
    util.pread2(cmd)

    if _is_mpath_daemon_running():
        # Flush the multipath nodes
        for sid in mpath_cli.list_maps():
            reset(sid,True)
        
        # ...then stop the daemon
        cmd = ["/etc/init.d/multipathd", "stop"]
        util.pread2(cmd)

    # Check the ISCSI daemon doesn't have any active sessions, if not,
    # restart in the new mode
    if iscsilib.is_iscsi_daemon_running() and not iscsilib._checkAnyTGT():
        iscsilib.restart_daemon()
        
    util.SMlog("MPATH: dm-multipath deactivated.")

def path(SCSIid):
    if _is_mpath_daemon_running():
        return MP_INUSEDIR + "/" + SCSIid
    else:
        return DEVBYIDPATH + "/scsi-" + SCSIid

def status(SCSIid):
    pass
