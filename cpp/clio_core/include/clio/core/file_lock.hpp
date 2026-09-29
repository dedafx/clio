#pragma once

#include <filesystem>

namespace clio::core {

/// An exclusive lock on a file, shared between threads, processes, and the
/// separate copies of clio_core inside the Python extension and the USD
/// plugin (design doc §10.3, "Shared state with Python Clio").
///
/// Blocks until the lock is acquired and releases it on destruction. Uses
/// flock() on POSIX (per open file, so two copies in one process also
/// exclude each other) and LockFileEx() on Windows. The lock file itself is
/// left in place.
class FileLock {
public:
    explicit FileLock(const std::filesystem::path& path);
    ~FileLock();

    FileLock(const FileLock&) = delete;
    FileLock& operator=(const FileLock&) = delete;

private:
#ifdef _WIN32
    void* _handle = nullptr;
#else
    int _fd = -1;
#endif
};

} // namespace clio::core
