#pragma once

#include "clio/core/identifier.hpp"
#include "clio/core/pin.hpp"

#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <exception>
#include <functional>
#include <future>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <utility>
#include <vector>

namespace clio::core {

/// Runs one piece of work per key at a time. A caller that asks for a key
/// while it is running waits for that run and gets its result (or its
/// exception) instead of starting another (design doc §10.3, "single-flight").
///
/// `Value` must be copyable. Thread-safe.
template <class Key, class Value>
class SingleFlight {
public:
    template <class Fn>
    Value run(const Key& key, Fn&& work) {
        std::promise<Value> promise;
        std::shared_future<Value> future;
        bool leader = false;
        {
            std::lock_guard<std::mutex> lock(_mutex);
            if (auto it = _flights.find(key); it != _flights.end()) {
                future = it->second;
            } else {
                future = promise.get_future().share();
                _flights.emplace(key, future);
                leader = true;
            }
        }
        if (!leader) {
            return future.get();
        }
        try {
            Value value = work();
            _forget(key);
            promise.set_value(value);
            return value;
        } catch (...) {
            _forget(key);
            promise.set_exception(std::current_exception());
            throw;
        }
    }

private:
    void _forget(const Key& key) {
        std::lock_guard<std::mutex> lock(_mutex);
        _flights.erase(key);
    }

    std::mutex _mutex;
    std::map<Key, std::shared_future<Value>> _flights;
};

/// Collects `sync` requests that arrive close together into one Perforce
/// command (design doc §10.3, "batching window").
///
/// USD loads sublayers and references on many threads, so a stage opening
/// 200 missing files would otherwise run 200 `p4 sync` commands. The first
/// caller for a given pin starts a batch and waits `window` for others to
/// join; it then runs one `sync` for all of them. Every caller blocks until
/// its batch is done and receives the batch's exception, if any. A request
/// for a file that is already in a running batch waits for that batch.
///
/// Thread-safe.
class SyncBatcher {
public:
    /// Syncs all `ids` to `pin` in one command. Throws on failure.
    using SyncFn = std::function<void(const std::vector<AssetIdentifier>& ids, const Pin& pin, bool force)>;

    /// A batch is also closed when it holds `maxBatch` files, so one command
    /// never carries an unbounded list.
    SyncBatcher(SyncFn sync, std::chrono::milliseconds window, std::size_t maxBatch = 500);

    /// Add `id` to a batch for (`pin`, `force`) and wait until it has run.
    void sync(const AssetIdentifier& id, const Pin& pin, bool force = false);

    /// How many batches have run. For tests and diagnostics.
    std::size_t batchesRun() const;

private:
    struct Batch {
        std::vector<AssetIdentifier> ids;
        std::set<std::string> seen;
        bool sealed = false;
        bool done = false;
        std::exception_ptr error;
    };

    void _seal(const std::string& key, const std::shared_ptr<Batch>& batch);

    SyncFn _sync;
    std::chrono::milliseconds _window;
    std::size_t _maxBatch;

    mutable std::mutex _mutex;
    std::condition_variable _changed;
    std::map<std::string, std::shared_ptr<Batch>> _open; ///< collecting, by key
    std::vector<std::pair<std::string, std::shared_ptr<Batch>>> _running;
    std::size_t _batchesRun = 0;
};

} // namespace clio::core
