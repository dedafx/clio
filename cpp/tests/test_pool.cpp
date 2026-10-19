// ConnectionPool tests. The first two need no server: a Connection does not
// open its socket until it runs a command.

#include "p4d_fixture.hpp"

#include "clio/core/p4/pool.hpp"

#include <doctest/doctest.h>

#include <atomic>
#include <chrono>
#include <thread>
#include <vector>

using namespace clio::core;
using clio::test::P4dFixture;

TEST_CASE("the pool never lends more than its maximum, and reuses connections") {
    p4::ConnectionPool pool(p4::ConnectionOptions{}, 3);
    std::atomic<int> inUse{0};
    std::atomic<int> peak{0};

    std::vector<std::thread> threads;
    for (int i = 0; i < 12; ++i) {
        threads.emplace_back([&] {
            for (int round = 0; round < 5; ++round) {
                auto lease = pool.acquire();
                const int now = ++inUse;
                int seen = peak.load();
                while (now > seen && !peak.compare_exchange_weak(seen, now)) {
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
                --inUse;
            }
        });
    }
    for (auto& t : threads) t.join();

    CHECK(peak.load() <= 3);
    CHECK(pool.createdConnections() <= 3);
    CHECK(pool.createdConnections() >= 1);
}

TEST_CASE("a waiting caller gets a connection when one is returned") {
    p4::ConnectionPool pool(p4::ConnectionOptions{}, 1);
    auto first = std::make_unique<p4::ConnectionPool::Lease>(pool.acquire());
    std::atomic<bool> got{false};
    std::thread waiter([&] {
        auto lease = pool.acquire();
        got = true;
    });
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    CHECK_FALSE(got.load());
    first.reset();
    waiter.join();
    CHECK(got.load());
    CHECK(pool.createdConnections() == 1);
}

TEST_CASE("pooled connections run commands in parallel") {
    auto fx = P4dFixture::create();
    if (!fx) {
        MESSAGE("CLIO_TEST_P4D is not set; skipping Perforce integration test");
        return;
    }
    p4::ConnectionPool pool(fx->settings().connection, 3);
    std::atomic<int> ok{0};
    std::vector<std::thread> threads;
    for (int i = 0; i < 6; ++i) {
        threads.emplace_back([&] {
            const auto info = pool.runOrThrow("info");
            if (info.records.size() == 1 && info.records[0].at("userName") == "clio_tester") ++ok;
        });
    }
    for (auto& t : threads) t.join();
    CHECK(ok.load() == 6);
    CHECK(pool.createdConnections() <= 3);
}
