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
import time, os, sys, re
import xs_errors
import lock
import mpath_cli

supported = ['iscsi','lvmoiscsi','hba','lvmohba','netapp','cslg']

LOCK_TYPE_HOST = "host"
LOCK_NS = "mpathcount"


util.daemon()
mpathcountlock = lock.Lock(LOCK_TYPE_HOST, LOCK_NS)
util.SMlog("MPATH: Trying to acquire the lock")
mpathcountlock.acquire()
util.SMlog("MPATH: I get the lock")

def mpc_exit(session, code):
    if session is not None:
        try:
            session.xenapi.logout()
        except:
            pass
    sys.exit(code)

def match_host_id(s):
    regex = re.compile("^INSTALLATION_UUID")
    return regex.search(s, 0)

def get_localhost_uuid():
    filename = '/etc/xensource-inventory'
    try:
        f = open(filename, 'r')
    except:
        raise xs_errors.XenError('EIO', \
              opterr="Unable to open inventory file [%s]" % filename)
    domid = ''
    for line in filter(match_host_id, f.readlines()):
        domid = line.split("'")[1]
    return domid

def match_dmpLUN(s):
    regex = re.compile("[0-9]*:[0-9]*:[0-9]*:[0-9]*")
    return regex.search(s, 0)

def match_pathup(s):
    s = re.sub('\]',' ',re.sub('\[','',s)).split()
    dm_status = s[-1]
    path_status = s[-2]
    for val in [dm_status, path_status]:
        if val in ['faulty','shaky','failed']:
            return False
    return True

def _tostring(l):
    return str(l)

def get_path_count(SCSIid, active=True):
    count = 0
    lines = mpath_cli.get_topology(SCSIid)
    for line in filter(match_dmpLUN,lines):
        if not active:
            count += 1
        elif match_pathup(line):
            count += 1
    return count

def update_config(session, pbdref, key, SCSIid, entry):
    util.SMlog("MPATH: Updating entry for [%s], current: %s" % (SCSIid,entry))
    if os.path.exists('/dev/mapper/' + SCSIid):
        count = get_path_count(SCSIid)
        total = get_path_count(SCSIid, active=False)
        newentry = [count, total]
        if str(newentry) != entry:
            session.xenapi.PBD.remove_from_other_config(pbdref,'multipathed')
            session.xenapi.PBD.remove_from_other_config(pbdref,key)
            session.xenapi.PBD.add_to_other_config(pbdref,'multipathed','true')
            session.xenapi.PBD.add_to_other_config(pbdref,key,str(newentry))
            util.SMlog("MPATH: \tSet val: %s" % str(newentry))

def get_SCSIidlist(devconfig, sm_config):
    SCSIidlist = []
    if sm_config.has_key('SCSIid'):
        SCSIidlist = sm_config['SCSIid'].split(',')
    elif devconfig.has_key('SCSIid'):
        SCSIidlist.append(devconfig['SCSIid'])
    else:
        for key in sm_config:
            if util._isSCSIid(key):
                SCSIidlist.append(re.sub("^scsi-","",key))
    return SCSIidlist

try:
    session = util.get_localAPI_session()
except:
    print "Unable to open local XAPI session"
    sys.exit(-1)

localhost = session.xenapi.host.get_by_uuid(get_localhost_uuid())
# Check whether DMP Multipathing is enabled
try:
    hconf = session.xenapi.host.get_other_config(localhost)
    assert(hconf['multipathing'] == 'true')
    assert(hconf['multipathhandle'] == 'dmp')
except:
    mpc_exit(session,0)

match_bySCSIid = False
if len(sys.argv) == 2:
    match_bySCSIid = True
    SCSIid = sys.argv[1]

try:
    pbds = session.xenapi.PBD.get_all_records_where("field \"host\" = \"%s\"" % localhost)
except:
    mpc_exit(session,-1)

try:
    for pbd in pbds:
        record = pbds[pbd]
        config = record['other_config']
        SR = record['SR']
        srtype = session.xenapi.SR.get_type(SR)
        sruuid = session.xenapi.SR.get_uuid(SR)
        if srtype in supported:
            devconfig = record["device_config"]
            sm_config = session.xenapi.SR.get_sm_config(SR)
            SCSIidlist = get_SCSIidlist(devconfig, sm_config)
            if not len(SCSIidlist):
                continue
            for i in SCSIidlist:
                if match_bySCSIid and i != SCSIid:
                    continue
                util.SMlog("Matched SCSIid, updating %s" % i)
                key = "mpath-" + i
                if not config.has_key(key):
                    update_config(session,pbd,key,i,"")
                else:
                    update_config(session,pbd,key,i,config[key])
except:
    util.SMlog("MPATH: Failure updating db")
    mpc_exit(session, -1)
    
util.SMlog("MPATH: Update done")

mpc_exit(session,0)
