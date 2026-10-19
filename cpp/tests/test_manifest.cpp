// Version manifests (design doc §10.8): hashing, parsing, the manifest's
// folder, the pool of stored files, and resolving through a manifest. The
// first tests need no server; the rest need $CLIO_TEST_P4D.

#include "p4d_fixture.hpp"

#include "clio/core/error.hpp"
#include "clio/core/hash.hpp"
#include "clio/core/manifest.hpp"
#include "clio/core/resolver.hpp"

#include <doctest/doctest.h>

#include <algorithm>
#include <fstream>
#include <sstream>

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

// The JSON text of a manifest for the given files, as Python would write it:
// revision, digest, size and type come from the server.
std::string manifestFor(P4dFixture& fx, const std::vector<std::string>& files,
                        const std::string& depot = "//depot/proj") {
    std::string json = "{\n  \"clio_manifest\": 1,\n  \"depot\": \"" + depot + "\",\n  \"server\": \"test\",\n"
                       "  \"source_pin\": \"have\",\n  \"files\": {\n";
    bool first = true;
    for (const auto& file : files) {
        const auto result = fx.connection().runOrThrow("fstat", {"-Ol", depot + "/" + file});
        REQUIRE(result.records.size() == 1);
        const auto& r = result.records[0];
        if (!first) json += ",\n";
        first = false;
        json += "    \"" + file + "\": {\"rev\": " + r.at("headRev") + ", \"change\": " + r.at("headChange") +
                ", \"digest\": \"" + r.at("digest") + "\", \"size\": " + r.at("fileSize") +
                ", \"type\": \"" + r.at("headType") + "\"}";
    }
    return json + "\n  }\n}\n";
}

std::filesystem::path writeManifest(P4dFixture& fx, const std::string& name, const std::string& json) {
    const auto path = fx.workspaceRoot().parent_path() / name;
    std::ofstream(path, std::ios::binary) << json;
    return path;
}

Settings manifestSettings(P4dFixture& fx, const std::filesystem::path& manifest, Policy policy) {
    auto settings = fx.settings();
    settings.manifest = manifest;
    settings.policy = policy;
    return settings;
}

} // namespace

TEST_CASE("md5 and sha256 match the published test vectors") {
    CHECK(md5Hex("") == "D41D8CD98F00B204E9800998ECF8427E");
    CHECK(md5Hex("abc") == "900150983CD24FB0D6963F7D28E17F72");
    CHECK(md5Hex("The quick brown fox jumps over the lazy dog") == "9E107D9D372BB6826BD81D3542A419D6");
    CHECK(sha256Hex("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    CHECK(sha256Hex("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    // 56 bytes: the padding needs a second block.
    CHECK(sha256Hex("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq") ==
          "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
    CHECK(md5Hex(std::string(1000, 'a')) == "CABE45DCC9AE5B66BA86600CCA6B8BA8");

    // Hashing in pieces gives the same answer.
    Sha256 pieces;
    pieces.update("ab", 2);
    pieces.update("c", 1);
    CHECK(pieces.finishHex() == sha256Hex("abc"));
}

TEST_CASE("md5File can ignore carriage returns, also across read boundaries") {
    const auto dir = std::filesystem::temp_directory_path() / "clio-md5-test";
    std::filesystem::create_directories(dir);
    const auto crlf = dir / "crlf.txt";
    const auto lf = dir / "lf.txt";
    std::ofstream(crlf, std::ios::binary) << "one\r\ntwo\r\nthree";
    std::ofstream(lf, std::ios::binary) << "one\ntwo\nthree";
    CHECK(md5File(crlf, true) == md5File(lf, false));
    CHECK(md5File(crlf, false) != md5File(lf, false));

    // A "\r\n" split by the 1 MB read size, and a lone "\r" at the end.
    std::string big(1 << 20, 'x');
    big[(1 << 20) - 1] = '\r';
    big += "\nrest\r";
    std::string expected(1 << 20, 'x');
    expected.back() = '\n';
    expected += "rest\r";
    std::ofstream(dir / "big.txt", std::ios::binary) << big;
    CHECK(md5File(dir / "big.txt", true) == md5Hex(expected));

    CHECK_THROWS_AS(md5File(dir / "missing.txt"), Error);
    std::error_code ec;
    std::filesystem::remove_all(dir, ec);
}

TEST_CASE("a manifest parses, and has an id") {
    const std::string text = R"({
  "clio_manifest": 1, "created": "2026-10-20T10:00:00Z", "created_by": "sam@ws",
  "server": "ssl:p:1666", "depot": "//imagine/main", "root_layer": "shots/shot.usda",
  "source_pin": "have", "usd_version": "26.08",
  "files": {
    "shots/shot.usda": {"rev": 14, "change": 18230, "digest": "9f2c0000000000000000000000000000", "size": 2481, "type": "text"},
    "assets/geo \u00e9.usdc": {"rev": 3, "change": 17990, "digest": "C07E0000000000000000000000000000", "size": 48213377, "type": "binary+Fl"}
  }
})";
    const auto m = Manifest::parse(text);
    CHECK(m.depot() == "//imagine/main");
    CHECK(m.rootLayer() == "shots/shot.usda");
    CHECK(m.sourcePin() == "have");
    CHECK(m.createdBy() == "sam@ws");
    REQUIRE(m.entries().size() == 2);
    const auto* shot = m.find("shots/shot.usda");
    REQUIRE(shot);
    CHECK(shot->rev == 14);
    CHECK(shot->digest == "9F2C0000000000000000000000000000"); // upper-case, as Perforce prints it
    CHECK(shot->isText());
    CHECK(shot->digestCheckable());
    const auto* geo = m.find("assets/geo \xC3\xA9.usdc"); // \u00e9 decoded to UTF-8
    REQUIRE(geo);
    CHECK(geo->size == 48213377);
    CHECK_FALSE(geo->isText());
    CHECK(m.find("nope.usda") == nullptr);

    CHECK(m.sha256() == sha256Hex(text));
    CHECK(m.folderName() == "manifest-" + sha256Hex(text).substr(0, 16));
    CHECK(Manifest::parse(text + " ").sha256() != m.sha256()); // the id is of the exact text
}

TEST_CASE("a manifest that is not valid is refused with a reason") {
    const std::string head = R"({"clio_manifest": 1, "depot": "//d/main", "files": )";
    CHECK_NOTHROW(Manifest::parse(head + R"({"a.usda": {"rev": 1}}})"));
    const std::vector<std::string> bads = {
             R"({"clio_manifest": 2, "depot": "//d/main", "files": {}})",
             R"({"depot": "//d/main", "files": {}})",
             R"({"clio_manifest": 1, "files": {}})",
             R"({"clio_manifest": 1, "depot": "//d/main"})",
             head + R"({"a.usda": {}}})",
             head + R"({"a.usda": {"rev": 0}}})",
             head + R"({"a.usda": {"rev": "7"}}})",
             head + R"({"a.usda": 5}})",
             head + R"({"../a.usda": {"rev": 1}}})",
             head + R"({"/a.usda": {"rev": 1}}})",
             head + R"({"C:/a.usda": {"rev": 1}}})",
             head + R"({"a//b.usda": {"rev": 1}}})",
             head + R"({"a.usda": {"rev": 1}})",  // not closed
             "[]", "", "{", "not json"};
    for (const auto& bad : bads) {
        CAPTURE(bad);
        CHECK_THROWS_AS(Manifest::parse(bad), ConfigError);
    }
    CHECK_THROWS_AS(Manifest::load("/no/such/manifest.json"), ConfigError);
}

TEST_CASE("the manifest setting round-trips") {
    const auto s = Settings::parse("depot=//d/main;root=/w;store=/s;manifest=/jobs/551/m.json;policy=verify");
    CHECK(s.manifest == "/jobs/551/m.json");
    CHECK(Settings::parse(s.str()) == s);
    CHECK(s.str() == "depot=//d/main;root=/w;store=/s;policy=verify;manifest=/jobs/551/m.json");
}

TEST_CASE("fetching a manifest fills its folder, checked against the digests") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"a.usda", "line one\nline two\n"}, {"big.bin", std::string("\x00\x01\x02\xFF", 4) + "payload"}}, "v1");
    fx->submit({{"a.usda", "line one\nline two changed\n"}}, "v2");

    // The manifest names a.usda at revision 1, although the head is 2.
    const auto fstat1 = fx->connection().runOrThrow("fstat", {"-Ol", "//depot/proj/a.usda#1"});
    REQUIRE(fstat1.records.size() == 1);
    const auto& a1 = fstat1.records[0];
    const auto big = fx->connection().runOrThrow("fstat", {"-Ol", "//depot/proj/big.bin"}).records.at(0);
    const std::string text =
        "{\"clio_manifest\": 1, \"depot\": \"//depot/proj\", \"source_pin\": \"have\", \"files\": {"
        "\"a.usda\": {\"rev\": 1, \"change\": " + a1.at("headChange") + ", \"digest\": \"" + a1.at("digest") +
        "\", \"size\": " + a1.at("fileSize") + ", \"type\": \"text\"},"
        "\"big.bin\": {\"rev\": 1, \"digest\": \"" + big.at("digest") + "\", \"size\": " + big.at("fileSize") +
        ", \"type\": \"binary\"}}}";
    const auto path = writeManifest(*fx, "m1.json", text);
    fx->clearWorkspace();

    AssetResolver verify(manifestSettings(*fx, path, Policy::Verify));
    REQUIRE(verify.manifest());
    const auto id = AssetIdentifier::parse("clio:/a.usda");

    // Under verify nothing is fetched on demand: the manifest is fetched first.
    CHECK_FALSE(verify.resolve(id));

    const auto report = verify.workspace().fetchManifest(*verify.manifest(), 4);
    CHECK(report.failures.empty());
    CHECK(report.files == 2);
    CHECK(report.fetched == 2);
    CHECK(report.bytes > 0);

    verify.refresh();
    const auto resolved = verify.resolve(id);
    REQUIRE(resolved);
    CHECK(resolved->localPath == verify.workspace().manifestPath(*verify.manifest(), "a.usda"));
    CHECK(resolved->localPath.string().find(verify.manifest()->folderName()) != std::string::npos);
    CHECK(readFile(resolved->localPath) == "line one\nline two\n"); // revision 1, not the head
    CHECK_FALSE(std::filesystem::exists(fx->workspaceRoot() / "a.usda")); // the workspace is untouched
    CHECK(resolved->pin == Pin::revision(1));

    // Fetching again finds everything in place.
    const auto again = verify.workspace().fetchManifest(*verify.manifest(), 4);
    CHECK(again.present == 2);
    CHECK(again.fetched == 0);

    // The folder is recognized, so relative paths between its layers resolve.
    CHECK(verify.manages(resolved->localPath));
    const auto fromPath = verify.resolvePath(resolved->localPath);
    REQUIRE(fromPath);
    CHECK(fromPath->localPath == resolved->localPath);

    // Stored files are read-only.
    const auto perms = std::filesystem::status(resolved->localPath).permissions();
    CHECK((perms & std::filesystem::perms::owner_write) == std::filesystem::perms::none);
}

TEST_CASE("nothing outside the manifest is provided under verify; sync resolves it with a warning") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"in.usda", "in"}, {"out.usda", "out"}}, "v1");
    const auto path = writeManifest(*fx, "m2.json", manifestFor(*fx, {"in.usda"}));

    AssetResolver verify(manifestSettings(*fx, path, Policy::Verify));
    verify.workspace().fetchManifest(*verify.manifest(), 2);
    CHECK(verify.resolve(AssetIdentifier::parse("clio:/in.usda")));
    // out.usda is on disk in the workspace, but it is not in the manifest.
    REQUIRE(std::filesystem::exists(fx->workspaceRoot() / "out.usda"));
    CHECK_FALSE(verify.resolve(AssetIdentifier::parse("clio:/out.usda")));

    AssetResolver offline(manifestSettings(*fx, path, Policy::Offline));
    CHECK(offline.resolve(AssetIdentifier::parse("clio:/in.usda")));
    CHECK_FALSE(offline.resolve(AssetIdentifier::parse("clio:/out.usda")));

    AssetResolver sync(manifestSettings(*fx, path, Policy::Sync));
    const auto outside = sync.resolve(AssetIdentifier::parse("clio:/out.usda"));
    REQUIRE(outside);
    CHECK(outside->warning.find("is not in the manifest") != std::string::npos);
}

TEST_CASE("under sync a manifest file that is not stored yet is fetched when it is asked for") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"a.usda", "a one"}}, "v1");
    const auto path = writeManifest(*fx, "m3.json", manifestFor(*fx, {"a.usda"}));
    fx->submit({{"a.usda", "a two"}}, "v2");
    fx->clearWorkspace();

    AssetResolver sync(manifestSettings(*fx, path, Policy::Sync));
    const auto resolved = sync.resolve(AssetIdentifier::parse("clio:/a.usda"));
    REQUIRE(resolved);
    CHECK(resolved->warning.empty());
    CHECK(readFile(resolved->localPath) == "a one");
}

TEST_CASE("two manifests share a stored file") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"geo.bin", std::string("\x00\x01 shared geometry", 18)}, {"shot.usda", "shot v1"}}, "v1");
    const auto first = writeManifest(*fx, "m4a.json", manifestFor(*fx, {"geo.bin", "shot.usda"}));
    fx->submit({{"shot.usda", "shot v2"}}, "v2");
    const auto second = writeManifest(*fx, "m4b.json", manifestFor(*fx, {"geo.bin", "shot.usda"}));

    AssetResolver one(manifestSettings(*fx, first, Policy::Verify));
    const auto r1 = one.workspace().fetchManifest(*one.manifest(), 2);
    CHECK(r1.fetched == 2);

    AssetResolver two(manifestSettings(*fx, second, Policy::Verify));
    const auto r2 = two.workspace().fetchManifest(*two.manifest(), 2);
    CHECK(r2.failures.empty());
    CHECK(r2.fetched == 1); // only shot.usda is new
    CHECK(r2.linked == 1);  // geo.bin comes from the pool

    const auto a = one.workspace().manifestPath(*one.manifest(), "geo.bin");
    const auto b = two.workspace().manifestPath(*two.manifest(), "geo.bin");
    CHECK(a != b);
    CHECK(std::filesystem::file_size(a) == std::filesystem::file_size(b));
    CHECK(md5File(a) == md5File(b));
    CHECK(readFile(one.workspace().manifestPath(*one.manifest(), "shot.usda")) == "shot v1");
    CHECK(readFile(two.workspace().manifestPath(*two.manifest(), "shot.usda")) == "shot v2");
}

TEST_CASE("a digest that does not match is a failure and leaves nothing behind") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"a.usda", "a"}}, "v1");
    std::string json = manifestFor(*fx, {"a.usda"});
    const auto at = json.find("\"digest\": \"") + 11;
    json.replace(at, 4, "0000");
    const auto path = writeManifest(*fx, "m5.json", json);

    AssetResolver resolver(manifestSettings(*fx, path, Policy::Verify));
    const auto report = resolver.workspace().fetchManifest(*resolver.manifest(), 1);
    REQUIRE(report.failures.size() == 1);
    CHECK(report.failures[0].find("a.usda") == 0);
    CHECK(report.failures[0].find("has digest") != std::string::npos);
    CHECK_FALSE(std::filesystem::exists(resolver.workspace().manifestPath(*resolver.manifest(), "a.usda")));
    CHECK_FALSE(resolver.resolve(AssetIdentifier::parse("clio:/a.usda")));
}

TEST_CASE("check finds revisions that are gone or changed") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"keep.usda", "keep"}, {"gone.usda", "gone"}}, "v1");
    const auto good = writeManifest(*fx, "m6.json", manifestFor(*fx, {"keep.usda", "gone.usda"}));

    AssetResolver resolver(manifestSettings(*fx, good, Policy::Verify));
    CHECK(resolver.workspace().checkManifest(*resolver.manifest()).empty());

    // A revision that is not on the server (as after an obliterate, which
    // the throwaway server cannot do): gone.usda#9.
    std::string missing = manifestFor(*fx, {"keep.usda", "gone.usda"});
    const auto gone = missing.find("\"gone.usda\": {\"rev\": 1");
    REQUIRE(gone != std::string::npos);
    missing.replace(missing.find("1", gone + 20), 1, "9");
    const auto bad = writeManifest(*fx, "m6b.json", missing);
    AssetResolver broken(manifestSettings(*fx, bad, Policy::Verify));
    const auto problems = broken.workspace().checkManifest(*broken.manifest());
    REQUIRE(problems.size() == 1);
    CHECK(problems[0].find("gone.usda#9 is not on the server any more") == 0);

    std::string json = manifestFor(*fx, {"keep.usda"});
    json.replace(json.find("\"digest\": \"") + 11, 4, "ABCD");
    const auto changed = writeManifest(*fx, "m7.json", json);
    AssetResolver other(manifestSettings(*fx, changed, Policy::Verify));
    const auto differs = other.workspace().checkManifest(*other.manifest());
    REQUIRE(differs.size() == 1);
    CHECK(differs[0].find("different digest") != std::string::npos);
}

TEST_CASE("a manifest for another project is refused") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"a.usda", "a"}}, "v1");
    std::string json = manifestFor(*fx, {"a.usda"});
    json.replace(json.find("//depot/proj"), 12, "//other/proj");
    const auto path = writeManifest(*fx, "m8.json", json);
    CHECK_THROWS_AS(AssetResolver(manifestSettings(*fx, path, Policy::Verify)), ConfigError);
    CHECK_THROWS_AS(AssetResolver(manifestSettings(*fx, fx->workspaceRoot() / "no-such.json", Policy::Verify)),
                    ConfigError);
}
