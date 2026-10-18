#pragma once

#include "clio/core/p4/connection.hpp"

#include <condition_variable>
#include <cstddef>
#include <memory>
#include <mutex>
#include <vector>

namespace clio::core::p4 {

/// A small set of Perforce connections shared by many threads (design doc
/// §8.1 and §10.3).
///
/// A Connection is not thread-safe, so a thread borrows one with acquire()
/// and gives it back by letting the Lease go out of scope. Connections are
/// created on first need, up to `maxConnections`; when all are in use,
/// acquire() waits for one to come back. A connection that failed
/// reconnects on its next command, so a Lease can always be reused.
///
/// Thread-safe.
class ConnectionPool {
public:
    class Lease {
    public:
        Lease(Lease&& other) noexcept;
        Lease& operator=(Lease&&) = delete;
        Lease(const Lease&) = delete;
        Lease& operator=(const Lease&) = delete;
        ~Lease();

        Connection& operator*() const { return *_connection; }
        Connection* operator->() const { return _connection; }

    private:
        friend class ConnectionPool;
        Lease(ConnectionPool& pool, Connection& connection) : _pool(&pool), _connection(&connection) {}

        ConnectionPool* _pool;
        Connection* _connection;
    };

    /// `maxConnections` below 1 is treated as 1.
    explicit ConnectionPool(ConnectionOptions options, std::size_t maxConnections = 4);
    ~ConnectionPool();

    ConnectionPool(const ConnectionPool&) = delete;
    ConnectionPool& operator=(const ConnectionPool&) = delete;

    /// Borrow a connection, waiting if all of them are in use. The caller
    /// must drop every Lease before the pool is destroyed.
    Lease acquire();

    /// Run a command on a borrowed connection.
    CommandResult run(const std::string& command,
                      const std::vector<std::string>& args = {},
                      const std::optional<std::string>& input = std::nullopt);
    CommandResult runOrThrow(const std::string& command,
                             const std::vector<std::string>& args = {},
                             const std::optional<std::string>& input = std::nullopt);

    /// The P4PORT the connections use. Makes no server calls.
    std::string effectivePort();

    std::size_t maxConnections() const { return _max; }

    /// How many connections exist so far (they are created on demand).
    std::size_t createdConnections() const;

    /// Disconnect the idle connections. Busy ones are left alone.
    void disconnectIdle();

private:
    void _release(Connection& connection);

    ConnectionOptions _options;
    std::size_t _max;
    mutable std::mutex _mutex;
    std::condition_variable _available;
    std::vector<std::unique_ptr<Connection>> _all;
    std::vector<Connection*> _idle;
};

} // namespace clio::core::p4
