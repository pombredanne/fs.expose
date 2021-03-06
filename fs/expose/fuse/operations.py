# coding: utf-8
from __future__ import absolute_import
from __future__ import unicode_literals

import os
import errno
import operator
import posix
import stat
import time
import weakref
import itertools

import six
import fuse

from ... import errors
from ...enums import ResourceType, Seek
from ...opener import open_fs
from ...path import basename, isparent, recursepath
from ...permissions import Permissions

from .utils import convert_fs_errors, timestamp


class PyfilesystemFuseOperations(fuse.Operations):

    @staticmethod
    def _stat_from_info(info):

        if info.has_namespace('stat'):
            return info.raw['stat']

        result = {}
        umask = os.umask(0)
        os.umask(umask)

        if info.has_namespace('details'):
            if info.accessed is not None:
                result['st_atime'] = int(timestamp(info.accessed))
            if info.modified is not None:
                result['st_mtime'] = int(timestamp(info.modified))
            if (info.created or info.metadata_changed) is not None:
                result['st_ctime'] = int(timestamp(info.created or info.metadata_changed))
            if info.size is not None:
                result['st_size'] = info.size
            # TODO: support links ?
            # if info.is_link:
            #     result['st_mode'] = stat.S_IFLNK
            if info.type is ResourceType.directory:
                result['st_mode'] = stat.S_IFDIR
            else:
                result['st_mode'] = stat.S_IFREG


        mode = result.get('st_mode', 0)

        if info.has_namespace('access'):
            if info.uid is not None:
                result['st_uid'] = info.uid
            if info.gid is not None:
                result['st_gid'] = info.gid
            if info.permissions is not None:
                result['st_mode'] = mode | info.permissions.mode
            elif info.has_namespace('details'):
                if info.type is ResourceType.directory:
                    result['st_mode'] = mode | 0o777 & ~umask
                else:
                    result['st_mode'] = mode | 0o666 & ~umask

        if info.name is not None and info.name in '/':
            result['st_nlink'] = 2

        return result

    def __init__(self, filesystem):
        self.descriptors = {}
        self.fs = open_fs(filesystem)

    def __call__(self, op, *args):
        op_method = getattr(self, op, None)
        if op_method is None:
            raise fuse.FuseOSError(errno.ENOSYS)
        return op_method(*args)

    def _getfd(self):
        return next(x for x in itertools.count() if x not in self.descriptors)

    @convert_fs_errors
    def chmod(self, path, mode):
        self.fs.setinfo(path, {'access': {'permissions': Permissions(mode=mode)}})

    @convert_fs_errors
    def chown(self, path, uid, gid):
        self.fs.setinfo(path, {'access': {'uid': uid, 'gid': gid}})

    @convert_fs_errors
    def create(self, path, mode, fi=None):
        exclusive = (posix.O_EXCL & mode)
        if not self.fs.create(path) and exclusive:
            raise errors.FileExists(path)
        return self.open(path, mode)

    @convert_fs_errors
    def destroy(self, path):
        for handle in self.descriptors.values():
            handle.close()
        self.descriptors.clear()
        self.fs.close()

    @convert_fs_errors
    def flush(self, path, fd):
        self.descriptors[fd].flush()

    @convert_fs_errors
    def getattr(self, path, fh=None):
        info = self.fs.getinfo(path, ['details', 'access', 'stat', 'link'])
        return self._stat_from_info(info)

    @convert_fs_errors
    def getxattr(self, path, name, position=0):
        raise fuse.FuseOSError(errno.ENOTSUP)

    @convert_fs_errors
    def mkdir(self, path, mode):
        self.fs.makedir(path)

    def link(self, target, source):
        raise fuse.FuseOSError(errno.EPERM)

    @convert_fs_errors
    def open(self, path, flags):
        # if write only -> check if appending or not
        if (flags & posix.O_ACCMODE) == posix.O_WRONLY:
            mode = 'a' if (flags & posix.O_APPEND) else 'w'
        # if read/write -> check if truncating or not
        elif (flags & posix.O_ACCMODE) == posix.O_RDWR:
            mode = 'w+' if (flags & (posix.O_TRUNC)) else 'r+'
        # if read-only -> check if actually writing (stat flags or truncating)
        elif (flags & posix.O_ACCMODE) == posix.O_RDONLY:
            mode = 'r+' if (flags & (posix.ST_WRITE | posix.O_TRUNC)) else 'r'
        fd = self._getfd()
        self.descriptors[fd] = self.fs.openbin(path, mode)
        return fd

    @convert_fs_errors
    def read(self, path, size, offset, fd):
        handle = self.descriptors[fd]
        if not handle.readable():
            raise fuse.FuseOSError(errno.EINVAL)
        handle.seek(offset, Seek.set)
        return handle.read(size)

    @convert_fs_errors
    def readdir(self, path, fh):
        return ['.', '..'] + [
            (info.name, self._stat_from_info(info), 0)
                for info in self.fs.scandir(
                    path, ['basic', 'access', 'details', 'link', 'stat']
                )
        ]

    @convert_fs_errors
    def release(self, path, fd):
        self.descriptors.pop(fd).close()

    @convert_fs_errors
    def rename(self, old, new):
        _old = self.fs.validatepath(old)
        _new = self.fs.validatepath(new)

        if _old in '/' or _new in '/':
            raise fuse.FuseOSError(errno.ENOENT)
        elif isparent(_old, _new):
            raise fuse.FuseOSError(errno.EINVAL)

        for component in recursepath(_old)[:-1]:
            if not self.fs.isdir(component):
                raise fuse.FuseOSError(errno.ENOTDIR)

        if self.fs.gettype(old) is ResourceType.directory:
            if not self.fs.exists(new):
                self.fs.makedir(new)
            if not self.fs.isempty(new):
                raise fuse.FuseOSError(errno.ENOTEMPTY)
            self.fs.movedir(old, new)
        else:
            if self.fs.isdir(new):
                raise fuse.FuseOSError(errno.EISDIR)
            self.fs.move(old, new)

    @convert_fs_errors
    def rmdir(self, path):
        for component in recursepath(path)[:-1]:
            if not self.fs.isdir(component):
                raise fuse.FuseOSError(errno.ENOTDIR)
        self.fs.removedir(path)

    @convert_fs_errors
    def statfs(self, path):
        statfs, meta = {}, self.fs.getmeta()
        if 'max_sys_path_length' in meta:
            return {'f_namelen': meta['max_sys_path_length']}
        return {}

    def symlink(self, target, source):
        # TODO?: support symlink
        raise fuse.FuseOSError(errno.EPERM)

    @convert_fs_errors
    def truncate(self, path, length, fd=None):
        _fd = fd or self.open(path, posix.O_RDWR)
        fh = self.descriptors[_fd]
        try:
            if not fh.writable():
                raise fuse.FuseOSError(errno.EINVAL)
            fh.seek(0)
            fh.truncate(length)
        finally:
            if fd is None:
                self.release(path, _fd)

    @convert_fs_errors
    def unlink(self, path):
        for component in recursepath(path)[:-1]:
            if not self.fs.isdir(component):
                raise fuse.FuseOSError(errno.ENOTDIR)
        self.fs.remove(path)

    @convert_fs_errors
    def utimens(self, path, times=None):
        now = time.time()
        atime, mtime = (times[0], times[1]) if times else (now, now)
        info = {'details': {"accessed": atime, "modified": mtime}}
        self.fs.setinfo(path, info)

    @convert_fs_errors
    def write(self, path, data, offset, fd):
        fh = self.descriptors[fd]
        fh.seek(offset, Seek.set)
        if fh.writable():
            return fh.write(data)
        else:
            raise fuse.FuseOSError(errno.EINVAL)
