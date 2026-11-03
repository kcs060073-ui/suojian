"""Standard-library Windows Job ownership for bounded, shell-free tool probes.

The target starts suspended. Only stdin/stdout/stderr are inherited; a private
KILL_ON_JOB_CLOSE Job owns it before ResumeThread. Wait for the entire Job,
including descendants whose original parent has already exited.
"""
from __future__ import annotations

from collections.abc import Mapping
import ctypes
from ctypes import wintypes as w
import math
import os
from pathlib import Path
import subprocess
import threading
import time


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [('length', w.DWORD), ('descriptor', w.LPVOID), ('inherit', w.BOOL)]


class _BasicLimits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                ('active_limit', w.DWORD), ('affinity', ctypes.c_size_t),
                ('priority', w.DWORD), ('scheduling', w.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', _BasicLimits), ('io', ctypes.c_uint64 * 6),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [('times', ctypes.c_int64 * 4), ('faults', w.DWORD),
                ('total', w.DWORD), ('active', w.DWORD), ('terminated', w.DWORD)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [('cb', w.DWORD), ('reserved', w.LPWSTR), ('desktop', w.LPWSTR),
                ('title', w.LPWSTR), ('x', w.DWORD), ('y', w.DWORD),
                ('xsize', w.DWORD), ('ysize', w.DWORD), ('xchars', w.DWORD),
                ('ychars', w.DWORD), ('fill', w.DWORD), ('flags', w.DWORD),
                ('show', w.WORD), ('reserved_size', w.WORD),
                ('reserved_data', w.LPVOID), ('stdin', w.HANDLE),
                ('stdout', w.HANDLE), ('stderr', w.HANDLE)]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [('startup', _StartupInfo), ('attributes', w.LPVOID)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [('process', w.HANDLE), ('thread', w.HANDLE),
                ('pid', w.DWORD), ('tid', w.DWORD)]


def _load_api():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateJobObjectW': ([w.LPVOID, w.LPCWSTR], w.HANDLE),
        'SetInformationJobObject': ([w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD], w.BOOL),
        'QueryInformationJobObject': ([w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, w.LPVOID], w.BOOL),
        'AssignProcessToJobObject': ([w.HANDLE, w.HANDLE], w.BOOL),
        'TerminateJobObject': ([w.HANDLE, w.UINT], w.BOOL),
        'CreatePipe': ([ctypes.POINTER(w.HANDLE), ctypes.POINTER(w.HANDLE),
                        ctypes.POINTER(_SecurityAttributes), w.DWORD], w.BOOL),
        'SetHandleInformation': ([w.HANDLE, w.DWORD, w.DWORD], w.BOOL),
        'CreateFileW': ([w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(_SecurityAttributes),
                         w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
        'InitializeProcThreadAttributeList': ([w.LPVOID, w.DWORD, w.DWORD,
                                               ctypes.POINTER(ctypes.c_size_t)], w.BOOL),
        'UpdateProcThreadAttribute': ([w.LPVOID, w.DWORD, ctypes.c_size_t, w.LPVOID,
                                       ctypes.c_size_t, w.LPVOID, w.LPVOID], w.BOOL),
        'DeleteProcThreadAttributeList': ([w.LPVOID], None),
        'CreateProcessW': ([w.LPCWSTR, w.LPWSTR, w.LPVOID, w.LPVOID, w.BOOL, w.DWORD,
                            w.LPVOID, w.LPCWSTR, ctypes.POINTER(_StartupInfoEx),
                            ctypes.POINTER(_ProcessInfo)], w.BOOL),
        'ResumeThread': ([w.HANDLE], w.DWORD),
        'TerminateProcess': ([w.HANDLE, w.UINT], w.BOOL),
        'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD),
        'OpenProcess': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        'IsProcessInJob': ([w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
        'GetExitCodeProcess': ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
        'CloseHandle': ([w.HANDLE], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


_api = _load_api() if os.name == 'nt' else None


def _check(ok):
    if not ok:
        raise ctypes.WinError()


def _active_processes(job):
    info = _Accounting()
    _check(_api.QueryInformationJobObject(job, 1, ctypes.byref(info), ctypes.sizeof(info), None))
    return info.active


def _remember_job_members(job, members):
    # Query only this private Job. A PID is used only to obtain a wait handle;
    # verify current Job membership before retaining it, and never kill by PID.
    buffer = ctypes.create_string_buffer(65536)
    _check(_api.QueryInformationJobObject(job, 3, buffer, len(buffer), None))
    count = (w.DWORD * 2).from_buffer(buffer)[1]
    if count > (len(buffer) - 8) // ctypes.sizeof(ctypes.c_size_t):
        raise OSError('job membership exceeds bounded inspection')
    pids = (ctypes.c_size_t * count).from_buffer(buffer, 8)
    for pid in pids:
        previous = members.get(pid)
        if previous and _api.WaitForSingleObject(previous, 0) == 258:
            continue
        handle = _api.OpenProcess(0x100400, False, pid)  # SYNCHRONIZE | QUERY_INFORMATION
        if not handle:
            if ctypes.get_last_error() == 87:  # member already exited
                continue
            raise ctypes.WinError()
        try:
            in_job = w.BOOL()
            _check(_api.IsProcessInJob(handle, job, ctypes.byref(in_job)))
            if in_job.value:
                if previous:
                    _api.CloseHandle(previous)
                members[pid] = handle
                handle = None
        finally:
            if handle:
                _api.CloseHandle(handle)


def _bounded_text(raw, limit):
    text = raw.decode('utf-8', errors='replace')
    encoded = text.encode('utf-8')
    if len(encoded) > limit:
        return encoded[:limit].decode('utf-8', errors='ignore'), True
    return text, False


def run_windows_probe(command, *, cwd, timeout_seconds, max_output_bytes, env, stream_reader):
    """Return the existing digital-workflow result contract; never launch a shell."""
    job = None
    process = _ProcessInfo()
    handles = set()
    readers = []
    members = {}
    streams = []
    transferred_fds = []
    outputs = [{}, {}]
    attribute_buffer = None
    attributes_initialized = False
    finished = False
    try:
        if (not isinstance(command, (list, tuple)) or not command
                or not all(isinstance(x, str) and x and '\0' not in x for x in command)):
            raise ValueError('nonempty command without NUL required')
        executable, directory = Path(command[0]), Path(cwd)
        if '\0' in str(directory):
            raise ValueError('working directory contains NUL')
        if _api is None or not executable.is_absolute() or not directory.is_absolute():
            raise ValueError('absolute executable and working directory required')
        if (not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool)
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 3600
                or not isinstance(max_output_bytes, int) or isinstance(max_output_bytes, bool)
                or max_output_bytes <= 0):
            raise ValueError('finite timeout and positive byte limit required')
        environment = None
        if env is not None:
            if not isinstance(env, Mapping):
                raise ValueError('environment mapping required')
            entries = []
            for key, value in env.items():
                if (not isinstance(key, str) or not isinstance(value, str)
                        or not key or '=' in key or '\0' in key or '\0' in value):
                    raise ValueError('invalid environment')
                entries.append(key + '=' + value)
            entries.sort(key=str.casefold)
            environment = ctypes.create_unicode_buffer('\0'.join(entries) + '\0\0')
        deadline = time.monotonic() + timeout_seconds
        job = _api.CreateJobObjectW(None, None)
        _check(job)
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _check(_api.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)
        write_handles = []
        read_handles = []
        for _ in range(2):
            read, write = w.HANDLE(), w.HANDLE()
            _check(_api.CreatePipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(security), 0))
            handles.update((read.value, write.value))
            _check(_api.SetHandleInformation(read, 1, 0))
            read_handles.append(read.value)
            write_handles.append(write.value)
        stdin = _api.CreateFileW('NUL', 0x80000000, 3, ctypes.byref(security), 3, 0x80, None)
        if stdin == ctypes.c_void_p(-1).value:
            raise ctypes.WinError()
        _check(stdin)
        handles.add(stdin)
        size = ctypes.c_size_t()
        _api.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError()
        attribute_buffer = ctypes.create_string_buffer(size.value)
        _check(_api.InitializeProcThreadAttributeList(attribute_buffer, 1, 0, ctypes.byref(size)))
        attributes_initialized = True
        inherited = (w.HANDLE * 3)(stdin, *write_handles)
        _check(_api.UpdateProcThreadAttribute(attribute_buffer, 0, 0x20002, inherited,
                                              ctypes.sizeof(inherited), None, None))
        startup = _StartupInfoEx()
        startup.startup.cb = ctypes.sizeof(startup)
        startup.startup.flags = 0x101  # USESTDHANDLES | USESHOWWINDOW
        startup.startup.show = 0
        startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = inherited
        startup.attributes = ctypes.cast(attribute_buffer, w.LPVOID)
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        # EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW | CREATE_SUSPENDED |
        # CREATE_UNICODE_ENVIRONMENT. No BREAKAWAY flag and no shell fallback.
        _check(_api.CreateProcessW(str(executable), command_line, None, None, True,
                                    0x08080404, environment, str(directory),
                                    ctypes.byref(startup), ctypes.byref(process)))
        _check(_api.AssignProcessToJobObject(job, process.process))
        for handle in [stdin, *write_handles]:
            _check(_api.CloseHandle(handle))
            handles.remove(handle)
        import msvcrt
        def drain(stream, result):
            try:
                stream_reader(stream, max_output_bytes, result)
            except Exception:
                result['read_error'] = True
        for handle, result in zip(read_handles, outputs):
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            handles.remove(handle)  # ownership transferred to the file descriptor
            transferred_fds.append(fd)
            stream = os.fdopen(fd, 'rb', buffering=0)
            transferred_fds.remove(fd)  # stream now owns it
            streams.append(stream)
            thread = threading.Thread(target=drain, args=(stream, result), daemon=True)
            readers.append(thread)
            thread.start()
        if _api.ResumeThread(process.thread) == 0xFFFFFFFF:
            raise ctypes.WinError()
        _check(_api.CloseHandle(process.thread))
        process.thread = None
        timed_out = False
        while _active_processes(job):
            _remember_job_members(job, members)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _check(_api.TerminateJobObject(job, 1))
                break
            # The parent may have exited; polling Job accounting still owns its descendants.
            time.sleep(min(0.02, remaining))
        cleanup_deadline = time.monotonic() + 5
        while _active_processes(job):
            if time.monotonic() >= cleanup_deadline:
                raise OSError('job tree did not terminate')
            time.sleep(0.01)
        for handle in [process.process, *members.values()]:
            remaining_ms = max(0, int((cleanup_deadline - time.monotonic()) * 1000))
            _check(_api.WaitForSingleObject(handle, remaining_ms) == 0)
        exit_code = w.DWORD()
        _check(_api.GetExitCodeProcess(process.process, ctypes.byref(exit_code)))
        for thread in readers:
            if thread.ident is not None:
                thread.join(timeout=2)
        if any(thread.is_alive() for thread in readers) or any(x.get('read_error') for x in outputs):
            raise OSError('bounded output reader did not finish')
        stdout, stdout_expanded = _bounded_text(outputs[0].get('bytes', b''), max_output_bytes)
        stderr, stderr_expanded = _bounded_text(outputs[1].get('bytes', b''), max_output_bytes)
        finished = True
        return {'started': True, 'timed_out': timed_out,
                'returncode': None if timed_out else exit_code.value,
                'stdout': stdout, 'stderr': stderr,
                'stdout_truncated': bool(outputs[0].get('truncated')) or stdout_expanded,
                'stderr_truncated': bool(outputs[1].get('truncated')) or stderr_expanded}
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        return {'started': False, 'timed_out': False, 'returncode': None,
                'stdout': '', 'stderr': '', 'stdout_truncated': False,
                'stderr_truncated': False,
                'error': f'Windows controlled probe rejected ({type(error).__name__})'}
    finally:
        # A process not yet assigned is still suspended. Terminate by its owned
        # HANDLE, never by a reusable PID; closing the Job kills all assigned children.
        if not finished and process.process:
            _api.TerminateProcess(process.process, 1)
        if job:
            if not finished:
                _api.TerminateJobObject(job, 1)
            _api.CloseHandle(job)
        if process.process:
            _api.WaitForSingleObject(process.process, 5000)
        for thread in readers:
            if thread.ident is not None:
                thread.join(timeout=2)
        for stream in streams:
            if not any(thread.is_alive() for thread in readers):
                stream.close()
        for fd in transferred_fds:
            os.close(fd)
        for handle in [*handles, *members.values(), process.thread, process.process]:
            if handle:
                _api.CloseHandle(handle)
        if attributes_initialized:
            _api.DeleteProcThreadAttributeList(attribute_buffer)
