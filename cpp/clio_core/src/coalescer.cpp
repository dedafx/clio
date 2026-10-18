#include "clio/core/coalescer.hpp"

#include <algorithm>

namespace clio::core {

SyncBatcher::SyncBatcher(SyncFn sync, std::chrono::milliseconds window, std::size_t maxBatch)
    : _sync(std::move(sync)), _window(window), _maxBatch(std::max<std::size_t>(maxBatch, 1)) {}

std::size_t SyncBatcher::batchesRun() const {
    std::lock_guard<std::mutex> lock(_mutex);
    return _batchesRun;
}

void SyncBatcher::_seal(const std::string& key, const std::shared_ptr<Batch>& batch) {
    batch->sealed = true;
    if (auto it = _open.find(key); it != _open.end() && it->second == batch) {
        _open.erase(it);
    }
    _running.emplace_back(key, batch);
}

void SyncBatcher::sync(const AssetIdentifier& id, const Pin& pin, bool force) {
    const std::string key = pin.str() + (force ? "|force" : "");
    const std::string idKey = id.str();

    std::unique_lock<std::mutex> lock(_mutex);

    std::shared_ptr<Batch> batch;
    for (const auto& [runningKey, running] : _running) {
        if (runningKey == key && running->seen.count(idKey)) {
            batch = running; // already on its way: share that run
            break;
        }
    }

    bool leader = false;
    if (!batch) {
        auto& open = _open[key];
        if (!open) {
            open = std::make_shared<Batch>();
            leader = true;
        }
        batch = open;
        if (batch->seen.insert(idKey).second) {
            batch->ids.push_back(id);
        }
        if (!leader && batch->ids.size() >= _maxBatch) {
            _seal(key, batch);
            _changed.notify_all(); // wakes the leader early
        }
    }

    if (leader) {
        _changed.wait_for(lock, _window, [&] { return batch->sealed; });
        if (!batch->sealed) {
            _seal(key, batch);
        }
        lock.unlock();
        std::exception_ptr error;
        try {
            _sync(batch->ids, pin, force);
        } catch (...) {
            error = std::current_exception();
        }
        lock.lock();
        batch->error = error;
        batch->done = true;
        ++_batchesRun;
        _running.erase(std::remove_if(_running.begin(), _running.end(),
                                      [&](const auto& entry) { return entry.second == batch; }),
                       _running.end());
        _changed.notify_all();
    } else {
        _changed.wait(lock, [&] { return batch->done; });
    }

    if (batch->error) {
        std::rethrow_exception(batch->error);
    }
}

} // namespace clio::core
