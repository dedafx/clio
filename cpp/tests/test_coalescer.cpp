// Unit tests for SingleFlight and SyncBatcher. No server needed.

#include "clio/core/coalescer.hpp"

#include <doctest/doctest.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <stdexcept>
#include <thread>

using namespace clio::core;
using namespace std::chrono_literals;

namespace {

AssetIdentifier idOf(int n) {
    return AssetIdentifier::parse("clio:/props/file" + std::to_string(n) + ".usda");
}

// Run `work(i)` on `count` threads that start together.
template <class Fn>
void inParallel(int count, Fn work) {
    std::atomic<bool> go{false};
    std::vector<std::thread> threads;
    for (int i = 0; i < count; ++i) {
        threads.emplace_back([&, i] {
            while (!go.load()) {
                std::this_thread::yield();
            }
            work(i);
        });
    }
    go = true;
    for (auto& t : threads) {
        t.join();
    }
}

} // namespace

TEST_CASE("single-flight runs the work once for concurrent callers of one key") {
    SingleFlight<std::string, int> flight;
    std::atomic<int> runs{0};
    std::atomic<int> sum{0};
    inParallel(8, [&](int) {
        sum += flight.run("k", [&] {
            ++runs;
            std::this_thread::sleep_for(200ms);
            return 7;
        });
    });
    CHECK(runs.load() == 1);
    CHECK(sum.load() == 8 * 7);

    // A later call starts new work: nothing is remembered.
    CHECK(flight.run("k", [&] {
              ++runs;
              return 9;
          }) == 9);
    CHECK(runs.load() == 2);
}

TEST_CASE("single-flight keeps different keys apart and shares failures") {
    SingleFlight<int, int> flight;
    std::atomic<int> runs{0};
    inParallel(4, [&](int i) {
        CHECK(flight.run(i, [&] {
                  ++runs;
                  std::this_thread::sleep_for(20ms);
                  return i;
              }) == i);
    });
    CHECK(runs.load() == 4);

    std::atomic<int> failures{0};
    std::atomic<int> failedRuns{0};
    inParallel(6, [&](int) {
        try {
            flight.run(1, [&]() -> int {
                ++failedRuns;
                std::this_thread::sleep_for(200ms);
                throw std::runtime_error("boom");
            });
        } catch (const std::runtime_error&) {
            ++failures;
        }
    });
    CHECK(failedRuns.load() == 1);
    CHECK(failures.load() == 6);
    // The failure is not remembered.
    CHECK(flight.run(1, [] { return 3; }) == 3);
}

TEST_CASE("misses inside the window become one sync") {
    std::mutex m;
    std::vector<std::vector<std::string>> calls;
    SyncBatcher batcher(
        [&](const std::vector<AssetIdentifier>& ids, const Pin&, bool) {
            std::lock_guard<std::mutex> lock(m);
            auto& call = calls.emplace_back();
            for (const auto& id : ids) call.push_back(id.str());
        },
        1000ms);

    inParallel(10, [&](int i) { batcher.sync(idOf(i), Pin::latest()); });

    REQUIRE(calls.size() == 1);
    CHECK(calls[0].size() == 10);
    CHECK(batcher.batchesRun() == 1);
}

TEST_CASE("the same file asked twice is synced once; pins and force are kept apart") {
    std::mutex m;
    std::vector<std::pair<std::string, std::size_t>> calls; // pin+force, file count
    SyncBatcher batcher(
        [&](const std::vector<AssetIdentifier>& ids, const Pin& pin, bool force) {
            std::lock_guard<std::mutex> lock(m);
            calls.emplace_back(pin.str() + (force ? "!" : ""), ids.size());
        },
        1000ms);

    inParallel(6, [&](int i) {
        switch (i % 3) {
        case 0: batcher.sync(idOf(1), Pin::latest()); break;     // twice
        case 1: batcher.sync(idOf(2), Pin::latest()); break;     // twice
        case 2: batcher.sync(idOf(3), Pin::have(), true); break; // twice
        }
    });

    std::sort(calls.begin(), calls.end());
    REQUIRE(calls.size() == 2);
    CHECK(calls[0] == std::make_pair(std::string("have!"), std::size_t(1)));
    CHECK(calls[1] == std::make_pair(std::string("latest"), std::size_t(2)));
}

TEST_CASE("a batch failure reaches every waiting caller and is not remembered") {
    std::atomic<int> runs{0};
    std::atomic<bool> fail{true};
    SyncBatcher batcher(
        [&](const std::vector<AssetIdentifier>&, const Pin&, bool) {
            ++runs;
            if (fail) throw std::runtime_error("server down");
        },
        1000ms);

    std::atomic<int> failures{0};
    inParallel(5, [&](int i) {
        try {
            batcher.sync(idOf(i), Pin::latest());
        } catch (const std::runtime_error&) {
            ++failures;
        }
    });
    CHECK(runs.load() == 1);
    CHECK(failures.load() == 5);

    fail = false;
    CHECK_NOTHROW(batcher.sync(idOf(1), Pin::latest()));
    CHECK(runs.load() == 2);
}

TEST_CASE("a full batch runs without waiting for the whole window") {
    std::atomic<int> runs{0};
    SyncBatcher batcher([&](const std::vector<AssetIdentifier>&, const Pin&, bool) { ++runs; }, 30s,
                        /*maxBatch=*/4);
    const auto start = std::chrono::steady_clock::now();
    // The leader waits for the window; the fourth file fills the batch.
    inParallel(4, [&](int i) { batcher.sync(idOf(i), Pin::latest()); });
    CHECK(std::chrono::steady_clock::now() - start < 20s);
    CHECK(runs.load() == 1);
}

TEST_CASE("a zero window syncs at once") {
    std::atomic<int> runs{0};
    SyncBatcher batcher([&](const std::vector<AssetIdentifier>&, const Pin&, bool) { ++runs; }, 0ms);
    batcher.sync(idOf(1), Pin::latest());
    batcher.sync(idOf(2), Pin::latest());
    CHECK(runs.load() == 2);
}
