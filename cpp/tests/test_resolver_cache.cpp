// How the resolver remembers answers and groups its server work (design doc
// §8.3 and §10.3). Needs $CLIO_TEST_P4D; skipped without it.

#include "p4d_fixture.hpp"

#include "clio/core/resolver.hpp"

#include <doctest/doctest.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <fstream>
#include <sstream>
#include <thread>
#include <vector>

using namespace clio::core;
using clio::test::P4dFixture;

namespace {

std::string readFile(const std::filesystem::path& path) {
    std::ifstream in(path, std::ios::binary);
    std::ostringstream out;
    out << in.rdbuf();
    std::string text = out.str();
    text.erase(std::remove(text.begin(), text.end(), '\r'), text.end());
    return text;
}

std::unique_ptr<P4dFixture> fixtureOrSkip() {
    auto fx = P4dFixture::create();
    if (!fx) {
        MESSAGE("CLIO_TEST_P4D is not set; skipping Perforce integration test");
    }
    return fx;
}

// A clock the test moves by hand.
struct FakeClock {
    std::shared_ptr<std::chrono::steady_clock::time_point> now =
        std::make_shared<std::chrono::steady_clock::time_point>(std::chrono::steady_clock::now());

    AssetResolver::Clock clock() const {
        auto shared = now;
        return [shared] { return *shared; };
    }
    void advance(std::chrono::seconds by) { *now += by; }
};

} // namespace

TEST_CASE("latest is trusted for the cache time, then checked again") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"a.usda", "v1"}}, "v1");
    fx->clearWorkspace();

    auto settings = fx->settings();
    settings.pin = Pin::latest();
    settings.cacheTtl = std::chrono::seconds(30);
    FakeClock time;
    AssetResolver resolver(settings, time.clock());
    const auto id = AssetIdentifier::parse("clio:/a.usda");

    REQUIRE(resolver.resolve(id));
    CHECK(readFile(fx->workspaceRoot() / "a.usda") == "v1");
    const auto syncs = resolver.syncCommandsRun();

    // A new version appears on the server (and the workspace goes back to
    // v1). Within the cache time the answer is remembered, so the server is
    // not asked and the old file stays.
    fx->submit({{"a.usda", "v2"}}, "v2");
    fx->connection().runOrThrow("sync", {"-q", "//depot/proj/a.usda#1"});
    time.advance(std::chrono::seconds(20));
    REQUIRE(resolver.resolve(id));
    CHECK(resolver.syncCommandsRun() == syncs);
    CHECK(readFile(fx->workspaceRoot() / "a.usda") == "v1");

    // Once the time has passed, the server is asked again.
    time.advance(std::chrono::seconds(11));
    REQUIRE(resolver.resolve(id));
    CHECK(resolver.syncCommandsRun() == syncs + 1);
    CHECK(readFile(fx->workspaceRoot() / "a.usda") == "v2");
}

TEST_CASE("a zero cache time checks every time; refresh checks at once") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"a.usda", "v1"}}, "v1");
    fx->clearWorkspace();

    auto settings = fx->settings();
    settings.pin = Pin::latest();
    settings.cacheTtl = std::chrono::seconds(0);
    AssetResolver always(settings);
    const auto id = AssetIdentifier::parse("clio:/a.usda");
    REQUIRE(always.resolve(id));
    REQUIRE(always.resolve(id));
    CHECK(always.syncCommandsRun() == 2);

    settings.cacheTtl = std::chrono::seconds(3600);
    AssetResolver remembers(settings);
    REQUIRE(remembers.resolve(id));
    REQUIRE(remembers.resolve(id));
    CHECK(remembers.syncCommandsRun() == 1);
    remembers.refresh();
    REQUIRE(remembers.resolve(id));
    CHECK(remembers.syncCommandsRun() == 2);
}

TEST_CASE("changes and revisions are remembered past the cache time") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    const auto v1 = fx->submit({{"a.usda", "v1"}}, "v1");

    auto settings = fx->settings();
    settings.cacheTtl = std::chrono::seconds(1);
    FakeClock time;
    AssetResolver resolver(settings, time.clock());
    const auto id = AssetIdentifier::parse("clio:/a.usda?change=" + std::to_string(v1));

    const auto first = resolver.resolve(id);
    REQUIRE(first);
    // Delete the stored copy: only a remembered answer still returns a path.
    std::filesystem::permissions(first->localPath, std::filesystem::perms::owner_write,
                                 std::filesystem::perm_options::add);
    std::filesystem::remove(first->localPath);
    time.advance(std::chrono::hours(24));
    const auto again = resolver.resolve(id);
    REQUIRE(again);
    CHECK_FALSE(std::filesystem::exists(again->localPath));

    resolver.refresh();
    const auto refetched = resolver.resolve(id);
    REQUIRE(refetched);
    CHECK(readFile(refetched->localPath) == "v1");
}

TEST_CASE("a file missing on the server is looked for again after the cache time") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"a.usda", "a"}}, "a");

    auto settings = fx->settings();
    settings.pin = Pin::latest();
    settings.cacheTtl = std::chrono::seconds(30);
    FakeClock time;
    AssetResolver resolver(settings, time.clock());
    const auto id = AssetIdentifier::parse("clio:/later.usda");

    CHECK_FALSE(resolver.resolve(id));
    fx->submit({{"later.usda", "now here"}}, "later");
    fx->connection().runOrThrow("sync", {"-q", "//depot/proj/later.usda#none"});
    CHECK_FALSE(resolver.resolve(id)); // remembered "not found"
    time.advance(std::chrono::seconds(31));
    const auto found = resolver.resolve(id);
    REQUIRE(found);
    CHECK(readFile(found->localPath) == "now here");
}

TEST_CASE("misses from many threads become one sync") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    std::map<std::string, std::string> files;
    for (int i = 0; i < 12; ++i) {
        files["many/f" + std::to_string(i) + ".usda"] = "content " + std::to_string(i);
    }
    fx->submit(files, "twelve files");
    fx->clearWorkspace();

    auto settings = fx->settings();
    settings.batchWindow = std::chrono::milliseconds(1500); // generous: threads start slowly on a busy machine
    AssetResolver resolver(settings);

    std::atomic<bool> go{false};
    std::atomic<int> resolved{0};
    std::vector<std::thread> threads;
    for (int i = 0; i < 12; ++i) {
        threads.emplace_back([&, i] {
            while (!go.load()) {
                std::this_thread::yield();
            }
            const auto asset = resolver.resolve(AssetIdentifier::parse("clio:/many/f" + std::to_string(i) + ".usda"));
            if (asset && readFile(asset->localPath) == "content " + std::to_string(i)) {
                ++resolved;
            }
        });
    }
    go = true;
    for (auto& t : threads) t.join();

    CHECK(resolved.load() == 12);
    CHECK(resolver.syncCommandsRun() == 1);
}

TEST_CASE("concurrent resolves of one file share a single resolve") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"one.usda", "one"}}, "one");
    fx->clearWorkspace();

    auto settings = fx->settings();
    settings.batchWindow = std::chrono::milliseconds(300);
    AssetResolver resolver(settings);

    std::atomic<bool> go{false};
    std::atomic<int> resolved{0};
    std::vector<std::thread> threads;
    for (int i = 0; i < 8; ++i) {
        threads.emplace_back([&] {
            while (!go.load()) {
                std::this_thread::yield();
            }
            if (resolver.resolve(AssetIdentifier::parse("clio:/one.usda"))) ++resolved;
        });
    }
    go = true;
    for (auto& t : threads) t.join();
    CHECK(resolved.load() == 8);
    CHECK(resolver.syncCommandsRun() == 1);
}

TEST_CASE("historical versions are fetched on several connections at once") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    std::map<std::string, std::string> files;
    for (int i = 0; i < 8; ++i) {
        files["old/f" + std::to_string(i) + ".usda"] = "old " + std::to_string(i);
    }
    const auto change = fx->submit(files, "eight files");
    for (auto& [name, content] : files) content += " (new)";
    fx->submit(files, "edited");

    auto settings = fx->settings();
    settings.maxConnections = 3;
    AssetResolver resolver(settings);

    std::atomic<bool> go{false};
    std::atomic<int> right{0};
    std::vector<std::thread> threads;
    for (int i = 0; i < 8; ++i) {
        threads.emplace_back([&, i] {
            while (!go.load()) {
                std::this_thread::yield();
            }
            const auto asset = resolver.resolve(AssetIdentifier::parse(
                "clio:/old/f" + std::to_string(i) + ".usda?change=" + std::to_string(change)));
            if (asset && readFile(asset->localPath) == "old " + std::to_string(i)) ++right;
        });
    }
    go = true;
    for (auto& t : threads) t.join();
    CHECK(right.load() == 8);
}
