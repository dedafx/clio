#include "clio/core/p4/pool.hpp"

#include <algorithm>

namespace clio::core::p4 {

ConnectionPool::Lease::Lease(Lease&& other) noexcept
    : _pool(other._pool), _connection(other._connection) {
    other._pool = nullptr;
    other._connection = nullptr;
}

ConnectionPool::Lease::~Lease() {
    if (_pool) {
        _pool->_release(*_connection);
    }
}

ConnectionPool::ConnectionPool(ConnectionOptions options, std::size_t maxConnections)
    : _options(std::move(options)), _max(std::max<std::size_t>(maxConnections, 1)) {}

ConnectionPool::~ConnectionPool() = default;

ConnectionPool::Lease ConnectionPool::acquire() {
    std::unique_lock<std::mutex> lock(_mutex);
    for (;;) {
        if (!_idle.empty()) {
            Connection* connection = _idle.back();
            _idle.pop_back();
            return Lease(*this, *connection);
        }
        if (_all.size() < _max) {
            // Constructing a Connection does not touch the network.
            _all.push_back(std::make_unique<Connection>(_options));
            return Lease(*this, *_all.back());
        }
        _available.wait(lock);
    }
}

void ConnectionPool::_release(Connection& connection) {
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _idle.push_back(&connection);
    }
    _available.notify_one();
}

CommandResult ConnectionPool::run(const std::string& command, const std::vector<std::string>& args,
                                  const std::optional<std::string>& input) {
    auto lease = acquire();
    return lease->run(command, args, input);
}

CommandResult ConnectionPool::runOrThrow(const std::string& command,
                                         const std::vector<std::string>& args,
                                         const std::optional<std::string>& input) {
    auto lease = acquire();
    return lease->runOrThrow(command, args, input);
}

std::string ConnectionPool::effectivePort() {
    auto lease = acquire();
    return lease->effectivePort();
}

std::size_t ConnectionPool::createdConnections() const {
    std::lock_guard<std::mutex> lock(_mutex);
    return _all.size();
}

void ConnectionPool::disconnectIdle() {
    std::lock_guard<std::mutex> lock(_mutex);
    for (Connection* connection : _idle) {
        connection->disconnect();
    }
}

} // namespace clio::core::p4
