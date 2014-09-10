# Copyright (C) 2014 Linaro Limited
#
# Author: Tyler Baker <tyler.baker@linaro.org>
# Author: Antonio Terceiro <antonio.terceiro@linaro.org>
# Derived From: dummy_drivers.py
#
# This file is part of LAVA Dispatcher.
#
# LAVA Dispatcher is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# LAVA Dispatcher is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along
# with this program; if not, see <http://www.gnu.org/licenses>.


import logging
import subprocess

from contextlib import contextmanager
from time import sleep
from lava_dispatcher.utils import finalize_process
from lava_dispatcher.errors import CriticalError
from lava_dispatcher.downloader import download_image
from lava_dispatcher.utils import (
    mkdtemp,
    connect_to_serial,
    extract_ramdisk,
    extract_modules,
    create_ramdisk,
)


def _call(context, cmd, ignore_failure, timeout):
    cmd = 'timeout -s SIGKILL ' + str(timeout) + 's ' + cmd
    context.run_command(cmd, failok=ignore_failure)


class FastBoot(object):

    def __init__(self, device):
        self.device = device
        self.context = device.context

    def __call__(self, args, ignore_failure=False, timeout=600):
        command = self.device.config.fastboot_command + ' ' + args
        command = "flock /var/lock/lava-fastboot.lck " + command
        _call(self.context, command, ignore_failure, timeout)

    def enter(self):
        try:
            # First we try a gentle reset
            self.device.adb(self.device.config.soft_boot_cmd)
        except subprocess.CalledProcessError:
            # Now a more brute force attempt. In this case the device is
            # probably hung.
            if self.device.config.hard_reset_command:
                logging.debug("Will hard reset the device")
                self.context.run_command(self.device.config.hard_reset_command)
            else:
                logging.critical(
                    "Hard reset command not configured. "
                    "Please reset the device manually."
                )

    def on(self):
        try:
            logging.info("Waiting for 10 seconds for connection to settle")
            sleep(10)
            self('getvar all', timeout=2)
            return True
        except subprocess.CalledProcessError:
            return False

    def erase(self, partition):
        self('erase %s' % partition)

    def flash(self, partition, image):
        self('flash %s %s' % (partition, image))

    def boot(self, image):
        # We need an extra bootloader reboot before actually booting the image
        # to avoid the phone entering charging mode and getting stuck.
        self('reboot')
        # specifically after `fastboot reset`, we have to wait a little
        sleep(10)
        self('boot %s' % image)


class BaseDriver(object):

    def __init__(self, device):
        self.device = device
        self.context = device.context
        self.config = device.config
        self.target_type = None
        self.scratch_dir = None
        self.fastboot = FastBoot(self)
        self._default_boot_cmds = 'boot_cmds_ramdisk'
        self._kernel = None
        self._ramdisk = None
        self._working_dir = None
        self.__boot_image__ = None

    # Public Methods

    def connect(self):
        """
        """
        raise NotImplementedError("connect")

    def enter_fastboot(self):
        """
        """
        raise NotImplementedError("enter_fastboot")

    def erase_boot(self):
        self.fastboot.erase('boot')

    def get_default_boot_cmds(self):
        return self._default_boot_cmds

    def boot(self, boot_cmds=None):
        if self.__boot_image__ is None:
            raise CriticalError('Deploy action must be run first')
        if self._kernel is not None:
            if self._ramdisk is not None:
                if self.config.fastboot_kernel_load_addr:
                    self.fastboot('boot -c "%s" -b %s %s %s' % (boot_cmds,
                                                                self.config.fastboot_kernel_load_addr,
                                                                self._kernel, self._ramdisk))
                else:
                    raise CriticalError('Kernel load address not defined!')
            else:
                if self.config.fastboot_kernel_load_addr:
                    self.fastboot('boot -c "%s" -b %s %s' % (boot_cmds,
                                                             self.config.fastboot_kernel_load_addr,
                                                             self._kernel))
                else:
                    raise CriticalError('Kernel load address not defined!')
        else:
            self.fastboot.boot(self.__boot_image__)

    def in_fastboot(self):
        if self.fastboot.on():
            logging.debug("Device is in fastboot mode - no need to hard reset")
            return True
        else:
            return False

    def finalize(self, proc):
        finalize_process(proc)

    def deploy_linaro_kernel(self, kernel, ramdisk, dtb, modules, rootfs, nfsrootfs,
                             bootloader, firmware, bl1, bl2, bl31, rootfstype,
                             bootloadertype, target_type, scratch_dir):
        self.target_type = target_type
        self.scratch_dir = scratch_dir
        if kernel is not None:
            self._kernel = self._get_image(kernel)
        else:
            raise CriticalError('A kernel image is required!')
        if ramdisk is not None:
            self._ramdisk = self._get_image(ramdisk)
            if modules is not None:
                modules = download_image(modules, self.context,
                                         self._working_dir,
                                         decompress=False)
                ramdisk_dir = extract_ramdisk(self._ramdisk, self.working_dir,
                                              is_uboot=False)
                extract_modules(modules, ramdisk_dir)
                self._ramdisk = create_ramdisk(ramdisk_dir, self._working_dir)
        if rootfs is not None:
            self._default_boot_cmds = 'boot_cmds_rootfs'
            rootfs = self._get_image(rootfs)
            self.fastboot.flash(self.config.rootfs_partition, rootfs)

        self.__boot_image__ = 'kernel'

    def deploy_android(self, boot, system, userdata, rootfstype,
                       bootloadertype, target_type, scratch_dir):
        self.target_type = target_type
        self.scratch_dir = scratch_dir
        self.erase_boot()
        if boot is not None:
            boot = self._get_image(boot)
        else:
            raise CriticalError('A boot image is required!')
        if system is not None:
            system = self._get_image(system)
            self.fastboot.flash('system', system)
        if userdata is not None:
            userdata = self._get_image(userdata)
            self.fastboot.flash('userdata', userdata)

        self.__boot_image__ = boot

    def adb(self, args, ignore_failure=False, spawn=False, timeout=600):
        cmd = self.config.adb_command + ' ' + args
        if spawn:
            return self.context.spawn(cmd, timeout=60)
        else:
            _call(self.context, cmd, ignore_failure, timeout)

    @property
    def working_dir(self):
        if self.config.shared_working_directory is None or \
                self.config.shared_working_directory.strip() == '':
            return self.scratch_dir

        if self._working_dir is None:
            self._working_dir = mkdtemp(self.config.shared_working_directory)
        return self._working_dir

    @contextmanager
    def adb_file_system(self, partition, directory):

        mount_point = self._get_partition_mount_point(partition)

        host_dir = '%s/mnt/%s' % (self.working_dir, directory)
        target_dir = '%s/%s' % (mount_point, directory)

        subprocess.check_call(['mkdir', '-p', host_dir])
        self.adb('pull %s %s' % (target_dir, host_dir), ignore_failure=True)

        yield host_dir

        self.adb('push %s %s' % (host_dir, target_dir))

    # Private Methods

    def _get_image(self, url):
        sdir = self.working_dir
        image = download_image(url, self.context, sdir, decompress=True)
        return image

    def _get_partition_mount_point(self, partition):
        lookup = {
            self.config.data_part_android_org: '/data',
            self.config.sys_part_android_org: '/system',
        }
        return lookup[partition]

class fastboot(BaseDriver):

    def __init__(self, device):
        super(fastboot, self).__init__(device)

    def deploy_linaro_kernel(self, kernel, ramdisk, dtb, modules, rootfs, nfsrootfs,
                             bootloader, firmware, bl1, bl2, bl31, rootfstype,
                             bootloadertype, target_type, scratch_dir):
        raise CriticalError('This platform does not support kernel deployment!')

    def enter_fastboot(self):
        self.fastboot.enter()

    def connect(self):
        if self.target_type == 'android':
            self.adb('wait-for-device')
            proc = self.adb('shell', spawn=True)
        else:
            raise CriticalError('This device only supports Android!')

        return proc


class nexus10(fastboot):

    def __init__(self, device):
        super(nexus10, self).__init__(device)

    def deploy_linaro_kernel(self, kernel, ramdisk, dtb, modules, rootfs, nfsrootfs,
                             bootloader, firmware, bl1, bl2, bl31, rootfstype,
                             bootloadertype, target_type, scratch_dir):
        raise CriticalError('This platform does not support kernel deployment!')

    def boot(self, boot_cmds=None):
        self.fastboot.flash('boot', self.__boot_image__)
        self.fastboot('reboot')


class fastboot_serial(BaseDriver):

    def __init__(self, device):
        super(fastboot_serial, self).__init__(device)

    def enter_fastboot(self):
        self.fastboot.enter()

    def connect(self):
        if self.config.connection_command:
            proc = connect_to_serial(self.context)
        else:
            raise CriticalError('The connection_command is not defined!')

        if self.target_type == 'android':
            self.adb('wait-for-device')

        return proc


class capri(fastboot_serial):

    def __init__(self, device):
        super(capri, self).__init__(device)

    def deploy_linaro_kernel(self, kernel, ramdisk, dtb, modules, rootfs, nfsrootfs,
                             bootloader, firmware, bl1, bl2, bl31, rootfstype, bootloadertype,
                             target_type, scratch_dir):
        raise CriticalError('This platform does not support kernel deployment!')

    def erase_boot(self):
        pass

    def boot(self, boot_cmds=None):
        self.fastboot.flash('boot', self.__boot_image__)
        self.fastboot('reboot')


class pxa1928dkb(fastboot_serial):

    def __init__(self, device):
        super(pxa1928dkb, self).__init__(device)

    def deploy_linaro_kernel(self, kernel, ramdisk, dtb, modules, rootfs, nfsrootfs,
                             bootloader, firmware, bl1, bl2, bl31, rootfstype, bootloadertype,
                             target_type, scratch_dir):
        raise CriticalError('This platform does not support kernel deployment!')

    def connect(self):
        if self.config.connection_command:
            proc = connect_to_serial(self.context)
        else:
            raise CriticalError('The connection_command is not defined!')

        return proc

    def erase_boot(self):
        pass

    def boot(self, boot_cmds=None):
        self.fastboot.flash('boot', self.__boot_image__)
        self.fastboot('reboot')

        if self.target_type == 'android':
            self.adb('wait-for-device')


class k3v2(fastboot_serial):

    def __init__(self, device):
        super(k3v2, self).__init__(device)

    def deploy_linaro_kernel(self, kernel, ramdisk, dtb, modules, rootfs, nfsrootfs,
                             bootloader, firmware, bl1, bl2, bl31, rootfstype, bootloadertype,
                             target_type, scratch_dir):
        raise CriticalError('This platform does not support kernel deployment!')

    def enter_fastboot(self):
        self.fastboot.enter()
        # Need to sleep and wait for the first stage bootloaders to initialize.
        sleep(10)

    def boot(self, boot_cmds=None):
        self.fastboot.flash('boot', self.__boot_image__)
        self.fastboot('reboot')
