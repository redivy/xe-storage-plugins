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
# SRCommand: parse SR command-line objects
#

import sys, errno, syslog
import xs_errors
import xmlrpclib
import SR, VDI, util

class SRCommand:
    def __init__(self, driver_info):
        self.dconf = ''
        self.type = ''
        self.sr_uuid = ''
        self.cmdname = ''
        self.cmdtype = ''
        self.cmd = None
        self.args = None
        self.driver_info = driver_info

    def parse(self):
        if len(sys.argv) <> 2:
            util.SMlog("Failed to parse commandline; wrong number of arguments; argv = %s" % (repr(sys.argv)))
            raise xs_errors.XenError('BadRequest') 
        try:
            params, methodname = xmlrpclib.loads(sys.argv[1])
            self.cmd = methodname
            params = params[0] # expect a single struct
            self.params = params

            # params is a dictionary
            self.dconf = params['device_config']
            if params.has_key('sr_uuid'):
                self.sr_uuid = params['sr_uuid']
            if params.has_key('vdi_uuid'):
                self.vdi_uuid = params['vdi_uuid']
                
        except Exception, e:
            util.SMlog("Failed to parse commandline; exception = %s argv = %s" % (str(e), repr(sys.argv)))
            raise xs_errors.XenError('BadRequest')

    def run_statics(self):
        if self.params['command'] == 'sr_get_driver_info':
            print util.sr_get_driver_info(self.driver_info)
            sys.exit(0)

    def run(self, sr):
        util.SMlog("%s %s" % (self.cmd, repr(self.params)))
        if self.cmd == 'vdi_create':
            self.vdi_uuid = util.gen_uuid ()
            target = sr.vdi(self.vdi_uuid)
            return target.create(self.params['sr_uuid'], self.vdi_uuid, long(self.params['args'][0]))

        elif self.cmd == 'vdi_update':
            target = sr.vdi(self.vdi_uuid)
            return target.update(self.params['sr_uuid'], self.vdi_uuid)

        elif self.cmd == 'vdi_introduce':
            target = sr.vdi(self.params['new_uuid'])
            return target.introduce(self.params['sr_uuid'], self.params['new_uuid'])
        
        elif self.cmd == 'vdi_delete':
            target = sr.vdi(self.vdi_uuid)
            return target.delete(self.params['sr_uuid'], self.vdi_uuid)

        elif self.cmd == 'vdi_attach':
            target = sr.vdi(self.vdi_uuid)
            return target.attach(self.params['sr_uuid'], self.vdi_uuid)

        elif self.cmd == 'vdi_detach':
            target = sr.vdi(self.vdi_uuid)
            return target.detach(self.params['sr_uuid'], self.vdi_uuid)

        elif self.cmd == 'vdi_snapshot':
            target = sr.vdi(self.vdi_uuid)
            return target.snapshot(self.params['sr_uuid'], self.vdi_uuid)                    

        elif self.cmd == 'vdi_clone':
            target = sr.vdi(self.vdi_uuid)
            return target.clone(self.params['sr_uuid'], self.vdi_uuid)            

        elif self.cmd == 'vdi_resize':
            target = sr.vdi(self.vdi_uuid)
            return target.resize(self.params['sr_uuid'], self.vdi_uuid, long(self.params['args'][0]))

        elif self.cmd == 'vdi_resize_online':
            target = sr.vdi(self.vdi_uuid)
            return target.resize_online(self.params['sr_uuid'], self.vdi_uuid, long(self.params['args'][0]))
        
        elif self.cmd == 'vdi_activate':
            target = sr.vdi(self.vdi_uuid)
            return target.activate(self.params['sr_uuid'], self.vdi_uuid)                    

        elif self.cmd == 'vdi_deactivate':
            target = sr.vdi(self.vdi_uuid)
            return target.deactivate(self.params['sr_uuid'], self.vdi_uuid)

        elif self.cmd == 'vdi_generate_config':
            target = sr.vdi(self.vdi_uuid)
            return target.generate_config(self.params['sr_uuid'], self.vdi_uuid)

        elif self.cmd == 'vdi_attach_from_config':
            target = sr.vdi(self.vdi_uuid)
            return target.attach_from_config(self.params['sr_uuid'], self.vdi_uuid)

        elif self.cmd == 'sr_create':
            return sr.create(self.params['sr_uuid'], long(self.params['args'][0]))

        elif self.cmd == 'sr_delete':
            return sr.delete(self.params['sr_uuid'])

        elif self.cmd == 'sr_update':
            return sr.update(self.params['sr_uuid'])

        elif self.cmd == 'sr_probe':
            txt = sr.probe()
            util.SMlog("sr_probe result: %s" % txt)
            # return the XML document as a string
            return xmlrpclib.dumps((txt,), "", True)

        elif self.cmd == 'sr_attach':
            # Schedule a scan only when attaching on the SRmaster
            if sr.dconf.has_key("SRmaster") and self.dconf["SRmaster"] == "true":
                util.set_dirty(sr.session, self.params["sr_ref"])

            return sr.attach(self.params['sr_uuid'])

        elif self.cmd == 'sr_detach':
            return sr.detach(self.params['sr_uuid'])

        elif self.cmd == 'sr_content_type':
            return sr.content_type(self.params['sr_uuid'])        

        elif self.cmd == 'sr_scan':
            return sr.scan(self.params['sr_uuid'])

        else:
            util.SMlog("Unknown command: %s" % self.cmd)
            raise xs_errors.XenError('BadRequest')             

def run(driver, driver_info):
    """Convenience method to run command on the given driver"""
    cmd = SRCommand(driver_info)
    try:
        cmd.parse()
        cmd.run_statics()
        sr = driver(cmd, cmd.sr_uuid)
        sr.direct = True
        ret = cmd.run(sr)

        if ret == None:
            print util.return_nil ()
        else:
            print ret
        sys.exit(0)
        
    except SR.SRException, inst:
        print inst.toxml()
        sys.exit(0)

        
