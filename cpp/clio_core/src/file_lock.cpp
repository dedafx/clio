#include "clio/core/file_lock.hpp"

#include "clio/core/error.hpp"

#include <system_error>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#else
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>
#endif

namespace clio::core {

FileLock::FileLock(const std::filesystem::path& path) {
    std::error_code ec;
    std::filesystem::create_directories(path.parent_path(), ec);
#ifdef _WIN32
    HANDLE handle = CreateFileW(path.wstring().c_str(), GENERIC_READ | GENERIC_WRITE,
                                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
                                OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (handle == INVALID_HANDLE_VALUE) {
        throw Error("Could not open lock file " + path.string());
    }
    OVERLAPPED overlapped{};
    if (!LockFileEx(handle, LOCKFILE_EXCLUSIVE_LOCK, 0, MAXDWORD, MAXDWORD, &overlapped)) {
        CloseHandle(handle);
        throw Error("Could not lock " + path.string());
    }
    _handle = handle;
#else
    _fd = ::open(path.c_str(), O_RDWR | O_CREAT | O_CLOEXEC, 0666);
    if (_fd < 0) {
        throw Error("Could not open lock file " + path.string() + ": " + std::strerror(errno));
    }
    int rc;
    do {
        rc = ::flock(_fd, LOCK_EX);
    } while (rc != 0 && errno == EINTR);
    if (rc != 0) {
        const std::string reason = std::strerror(errno);
        ::close(_fd);
        throw Error("Could not lock " + path.string() + ": " + reason);
    }
#endif
}

FileLock::~FileLock() {
#ifdef _WIN32
    if (_handle) {
        OVERLAPPED overlapped{};
        UnlockFileEx(static_cast<HANDLE>(_handle), 0, MAXDWORD, MAXDWORD, &overlapped);
        CloseHandle(static_cast<HANDLE>(_handle));
    }
#else
    if (_fd >= 0) {
        ::flock(_fd, LOCK_UN);
        ::close(_fd);
    }
#endif
}

} // namespace clio::core
