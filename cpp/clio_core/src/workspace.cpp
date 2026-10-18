#include "clio/core/workspace.hpp"

#include "clio/core/error.hpp"
#include "clio/core/file_lock.hpp"
#include "clio/core/hash.hpp"

#include <cstdint>
#include <cstdio>
#include <cstdlib>

#include <algorithm>
#include <atomic>
#include <thread>
#include <cctype>
#include <map>
#include <set>
#include <system_error>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <process.h>
#define CLIO_GETPID _getpid
#else
#include <unistd.h>
#define CLIO_GETPID getpid
#endif

namespace clio::core {

namespace {

// Stable 64-bit FNV-1a hash, the same in every build of clio_core.
std::uint64_t fnv1a64(const std::string& text) {
    std::uint64_t hash = 1469598103934665603ull;
    for (unsigned char c : text) {
        hash = (hash ^ c) * 1099511628211ull;
    }
    return hash;
}

// Reversible, collision-free folder name for any text: bytes outside
// [a-z0-9_.-] become %XX. ("approved:prod" -> "approved%3Aprod", while
// "approved_prod" stays as it is.) Upper case is encoded too, so names that
// differ only in case ("Prod", "prod") get different folders on
// case-insensitive file systems (Windows, macOS).
std::string encodeDirName(const std::string& text) {
    static const char* hex = "0123456789ABCDEF";
    std::string out;
    for (unsigned char c : text) {
        const bool ok = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
                        c == '-' || c == '_' || c == '.';
        if (ok) {
            out += static_cast<char>(c);
        } else {
            out += '%';
            out += hex[c >> 4];
            out += hex[c & 0xF];
        }
    }
    return out;
}

// Folder name for a server or depot root. Short names are encodeDirName();
// long ones (an rsh: port can be a whole command line) keep a readable
// prefix plus a hash of the full text, so paths stay within Windows limits.
// '~' is never produced by encodeDirName, so the two forms cannot collide.
std::string namespaceDirName(const std::string& text) {
    constexpr std::size_t maxLength = 64;
    std::string encoded = encodeDirName(text);
    if (encoded.empty()) {
        return "default";
    }
    if (encoded.size() <= maxLength) {
        return encoded;
    }
    std::size_t cut = 40;
    // Do not split a %XX escape.
    if (const std::size_t pct = encoded.rfind('%', cut - 1); pct != std::string::npos && pct + 3 > cut) {
        cut = pct;
    }
    char hash[17];
    std::snprintf(hash, sizeof(hash), "%016llx", static_cast<unsigned long long>(fnv1a64(text)));
    return encoded.substr(0, cut) + "~" + hash;
}

// `root` joined with an asset's project path, one segment at a time. A
// segment that std::filesystem would treat as a new root (a drive, for
// example) is refused, so the result is always inside `root`. Identifiers
// already reject these; this keeps the guarantee local to path building.
std::filesystem::path underRoot(std::filesystem::path root, const AssetIdentifier& id) {
    const std::string rel = id.relativePath();
    std::size_t start = 0;
    while (start <= rel.size()) {
        std::size_t end = rel.find('/', start);
        if (end == std::string::npos) {
            end = rel.size();
        }
        const std::filesystem::path segment(rel.substr(start, end - start));
        if (segment.has_root_name() || segment.has_root_directory() || segment == "..") {
            throw IdentifierError("Asset path '" + id.path() + "' leaves the project root");
        }
        root /= segment;
        start = end + 1;
    }
    return root;
}

std::optional<std::string> decodeLabel(const std::string& encoded) {
    std::string out;
    for (std::size_t i = 0; i < encoded.size(); ++i) {
        if (encoded[i] != '%') {
            out += encoded[i];
            continue;
        }
        if (i + 2 >= encoded.size()) {
            return std::nullopt; // truncated %XX
        }
        const auto hexValue = [](char h) -> int {
            if (h >= '0' && h <= '9') return h - '0';
            if (h >= 'A' && h <= 'F') return h - 'A' + 10;
            return -1;
        };
        const int hi = hexValue(encoded[i + 1]);
        const int lo = hexValue(encoded[i + 2]);
        if (hi < 0 || lo < 0) {
            return std::nullopt;
        }
        out += static_cast<char>(hi * 16 + lo);
        i += 2;
    }
    // Only the canonical encoding maps back, so each folder has one label.
    return encodeDirName(out) == encoded ? std::optional<std::string>(out) : std::nullopt;
}

std::string pinDirName(const Pin& pin) {
    switch (pin.kind()) {
    case Pin::Kind::Change:
        return "change-" + std::to_string(pin.number());
    case Pin::Kind::Label:
        return "label-" + encodeDirName(pin.labelName());
    case Pin::Kind::Revision:
        return "rev-" + std::to_string(pin.number());
    case Pin::Kind::Latest:
    case Pin::Kind::Have:
        break;
    }
    throw Error("Pin '" + pin.str() + "' is not a historical version");
}

// Whether two path components name the same entry. Windows paths are
// case-insensitive: USD may pass "c:/work/proj/..." for a root of "C:\Work\Proj".
bool sameComponent(const std::filesystem::path& a, const std::filesystem::path& b) {
#ifdef _WIN32
    const std::wstring& x = a.native();
    const std::wstring& y = b.native();
    return CompareStringOrdinal(x.c_str(), static_cast<int>(x.size()), y.c_str(),
                                static_cast<int>(y.size()), TRUE) == CSTR_EQUAL;
#else
    return a == b;
#endif
}

// `path` relative to `base` if it is inside it, using '/' separators. The
// result keeps the spelling of `path`.
std::optional<std::string> relativeInside(const std::filesystem::path& path,
                                          const std::filesystem::path& base) {
    if (base.empty() || !path.is_absolute()) {
        return std::nullopt;
    }
    const auto normPath = path.lexically_normal();
    const auto normBase = base.lexically_normal();
    auto it = normPath.begin();
    for (const auto& part : normBase) {
        if (part.empty()) {
            continue; // trailing separator
        }
        if (it == normPath.end() || !sameComponent(*it, part)) {
            return std::nullopt;
        }
        ++it;
    }
    std::filesystem::path rel;
    for (; it != normPath.end(); ++it) {
        if (!it->empty()) {
            rel /= *it;
        }
    }
    const std::string text = rel.generic_string();
    if (text.empty() || text == "." || text == ".." || text.rfind("../", 0) == 0) {
        return std::nullopt;
    }
    return text;
}

std::optional<Pin> pinFromDirName(const std::string& name) {
    try {
        if (name.rfind("change-", 0) == 0) return Pin::parse("@" + name.substr(7));
        if (name.rfind("label-", 0) == 0) {
            const auto label = decodeLabel(name.substr(6));
            return label ? std::optional<Pin>(Pin::label(*label)) : std::nullopt;
        }
        if (name.rfind("rev-", 0) == 0) return Pin::parse("#" + name.substr(4));
    } catch (const Error&) {
    }
    return std::nullopt;
}

bool deletedAtHead(const p4::Record& record) {
    const auto it = record.find("headAction");
    return it != record.end() &&
           (it->second == "delete" || it->second == "move/delete" || it->second == "purge" ||
            it->second == "archive");
}

// Values of a numbered tagged field: otherOpen0, otherOpen1, ... or, with
// one holder, the field itself.
std::vector<std::string> numberedField(const p4::Record& record, const std::string& name) {
    std::vector<std::string> values;
    for (int i = 0;; ++i) {
        const auto it = record.find(name + std::to_string(i));
        if (it == record.end()) {
            break;
        }
        values.push_back(it->second);
    }
    return values;
}

// "sam@sam_ws" -> "sam (workspace sam_ws)"
std::string holderText(const std::string& userAtClient) {
    const auto at = userAtClient.find('@');
    if (at == std::string::npos) {
        return userAtClient;
    }
    return userAtClient.substr(0, at) + " (workspace " + userAtClient.substr(at + 1) + ")";
}

} // namespace

Workspace::Workspace(Settings settings)
    : _settings(std::move(settings)), _pool(_settings.connection, _settings.maxConnections) {
    // Name the server's store folder by the port actually used, so different
    // servers taken from the environment never share a folder. (Equivalent
    // aliases for one server get separate folders, which is safe.)
    std::string port = _settings.connection.port;
    if (port.empty()) {
        try {
            port = _pool.effectivePort();
        } catch (const Error&) {
        }
    }
    _serverKey = namespaceDirName(port);
}

std::filesystem::path Workspace::lockPath() const {
    const std::string key = _serverKey + "\n" + _settings.connection.client + "\n" +
                            _settings.workspaceRoot.lexically_normal().generic_string();
    char name[32];
    std::snprintf(name, sizeof(name), "%016llx.lock", static_cast<unsigned long long>(fnv1a64(key)));
    return Settings::defaultCacheDir() / "locks" / name;
}

std::string Workspace::depotPath(const AssetIdentifier& id) const {
    // Perforce reserves @ # % * in file names; they are written as %XX.
    std::string path = _settings.depotRoot;
    for (const char c : id.path()) {
        switch (c) {
        case '@': path += "%40"; break;
        case '#': path += "%23"; break;
        case '%': path += "%25"; break;
        case '*': path += "%2A"; break;
        default: path += c; break;
        }
    }
    return path;
}

std::filesystem::path Workspace::workspacePath(const AssetIdentifier& id) const {
    return underRoot(_settings.workspaceRoot, id);
}

std::filesystem::path Workspace::_storeBase() const {
    return _settings.versionStore / _serverKey / namespaceDirName(_settings.depotRoot.substr(2));
}

std::filesystem::path Workspace::versionStorePath(const AssetIdentifier& id, const Pin& pin) const {
    return underRoot(_storeBase() / pinDirName(pin), id);
}

std::optional<LocalPathMatch> Workspace::matchLocalPath(const std::filesystem::path& path) const {
    const auto storeBase = _storeBase();
    if (auto rel = relativeInside(path, storeBase)) {
        const std::size_t slash = rel->find('/');
        if (slash == std::string::npos) {
            return std::nullopt;
        }
        if (!_manifestFolder.empty() && rel->substr(0, slash) == _manifestFolder) {
            try {
                return LocalPathMatch{AssetIdentifier::fromRelativePath(rel->substr(slash + 1)), std::nullopt, true, true};
            } catch (const Error&) {
                return std::nullopt;
            }
        }
        auto pin = pinFromDirName(rel->substr(0, slash));
        if (!pin) {
            return std::nullopt;
        }
        try {
            return LocalPathMatch{AssetIdentifier::fromRelativePath(rel->substr(slash + 1), pin), pin, true};
        } catch (const Error&) {
            return std::nullopt;
        }
    }
    if (auto rel = relativeInside(path, _settings.workspaceRoot)) {
        try {
            return LocalPathMatch{AssetIdentifier::fromRelativePath(*rel), std::nullopt, false};
        } catch (const Error&) {
            return std::nullopt;
        }
    }
    return std::nullopt;
}

p4::CommandResult Workspace::sync(const std::vector<AssetIdentifier>& ids, const Pin& pin, bool force) {
    if (pin.kind() == Pin::Kind::Revision) {
        throw Error("A revision pin is fetched into the version store, not synced");
    }
    std::vector<std::string> args{"-q"};
    if (force) {
        args.push_back("-f");
    }
    args.reserve(ids.size() + 1);
    for (const auto& id : ids) {
        args.push_back(depotPath(id) + pin.p4RevSpec());
    }
    std::lock_guard<std::mutex> lock(_syncMutex);
    // Other processes (a DCC's resolver, the clio CLI) may change the same
    // workspace; serialize with them too.
    FileLock workspaceLock(lockPath());
    return _pool.runOrThrow("sync", args);
}

WriteCheck Workspace::prepareForWrite(const AssetIdentifier& id) {
    const std::string depot = depotPath(id);
    const std::filesystem::path local = workspacePath(id);

    std::lock_guard<std::mutex> lock(_syncMutex);
    FileLock workspaceLock(lockPath());

    const auto fstat = _pool.runOrThrow("fstat", {depot});
    const p4::Record* record = fstat.records.empty() ? nullptr : &fstat.records.front();

    WriteCheck check;
    if (record && record->count("action")) {
        return check; // already open in this workspace: nothing to do
    }
    if (!record || deletedAtHead(*record)) {
        check.isNew = true;
        return check;
    }

    // Someone else's lock: an explicit one, or the exclusive open that a +l
    // file type takes.
    std::string holder;
    if (record->count("otherLock")) {
        const auto locks = numberedField(*record, "otherLock");
        const auto opens = numberedField(*record, "otherOpen");
        holder = !locks.empty() ? locks.front() : (!opens.empty() ? opens.front() : "another user");
    } else if (record->count("headType") && record->at("headType").find("+l") != std::string::npos) {
        const auto opens = numberedField(*record, "otherOpen");
        if (!opens.empty()) {
            holder = opens.front();
        }
    }
    if (!holder.empty()) {
        check.allowed = false;
        check.whyNot = id.path() + " is locked by " + holderText(holder);
        return check;
    }

    if (!record->count("haveRev")) {
        check.allowed = false;
        check.whyNot = id.path() + " is in Perforce but not in your workspace; get it before changing it";
        return check;
    }
    if (record->count("headRev") && record->at("headRev") != record->at("haveRev")) {
        check.warning = id.path() + ": your copy is not the latest version";
    }

    const auto edit = _pool.run("edit", {local.string()});
    const bool opened = std::any_of(edit.records.begin(), edit.records.end(),
                                    [](const p4::Record& r) { return r.count("action") > 0; });
    if (!opened) {
        check.allowed = false;
        check.whyNot = "Perforce did not open " + id.path() + " for edit";
        if (edit.hasErrors()) {
            check.whyNot += ": " + edit.errorText();
        }
    }
    return check;
}

std::string Workspace::addNewFile(const AssetIdentifier& id) {
    const std::filesystem::path local = workspacePath(id);
    std::error_code ec;
    if (!std::filesystem::exists(local, ec)) {
        return {}; // the write did not happen
    }
    std::lock_guard<std::mutex> lock(_syncMutex);
    FileLock workspaceLock(lockPath());
    // -f: add even if the file name has characters Perforce would warn about.
    const auto add = _pool.run("add", {"-f", local.string()});
    if (add.hasErrors()) {
        return id.path() + " was saved but not added to Perforce: " + add.errorText();
    }
    return {};
}

std::optional<HaveInfo> Workspace::haveInfo(const AssetIdentifier& id) {
    const auto fstat = _pool.runOrThrow("fstat", {"-T", "haveRev,action", depotPath(id)});
    if (fstat.records.empty()) {
        return std::nullopt;
    }
    const p4::Record& record = fstat.records.front();
    const auto have = record.find("haveRev");
    if (have == record.end() || have->second.empty() || have->second == "none") {
        return std::nullopt;
    }
    HaveInfo info;
    info.haveRev = std::strtol(have->second.c_str(), nullptr, 10);
    info.opened = record.count("action") > 0;
    return info;
}

bool Workspace::clientUsesModtime() {
    std::lock_guard<std::mutex> lock(_modtimeMutex);
    if (!_usesModtime) {
        const auto spec = _pool.runOrThrow("client", {"-o"});
        bool uses = false;
        if (!spec.records.empty()) {
            const auto options = spec.records.front().find("Options");
            if (options != spec.records.front().end()) {
                // "allwrite noclobber nocompress unlocked modtime rmdir": a
                // whole word, because "nomodtime" contains "modtime".
                const std::string& text = options->second;
                std::size_t at = 0;
                while ((at = text.find("modtime", at)) != std::string::npos) {
                    const bool wordStart = at == 0 || text[at - 1] == ' ';
                    const bool wordEnd = at + 7 == text.size() || text[at + 7] == ' ';
                    if (wordStart && wordEnd) {
                        uses = true;
                    }
                    at += 7;
                }
            }
        }
        _usesModtime = uses;
    }
    return *_usesModtime;
}

void Workspace::useManifest(const Manifest& manifest) {
    _manifestFolder = manifest.folderName();
}

std::filesystem::path Workspace::manifestPath(const Manifest& manifest, const std::string& relativePath) const {
    return underRoot(_storeBase() / manifest.folderName(), AssetIdentifier::fromRelativePath(relativePath));
}

Workspace::Materialized Workspace::materialize(const Manifest& manifest, const ManifestEntry& entry,
                                               bool verifyPresent, std::int64_t* bytesFetched) {
    namespace fs = std::filesystem;
    const fs::path target = manifestPath(manifest, entry.path);
    std::error_code ec;
    const bool check = entry.digestCheckable();

    if (fs::exists(target, ec)) {
        if (!verifyPresent || !check || md5File(target, entry.isText()) == entry.digest) {
            return Materialized::Present;
        }
        // Damaged or replaced: put the right file back.
        fs::permissions(target, fs::perms::owner_write, fs::perm_options::add, ec);
        fs::remove(target, ec);
    }
    fs::create_directories(target.parent_path(), ec);
    if (ec) {
        throw Error("Could not create " + target.parent_path().string() + ": " + ec.message());
    }

    const auto readOnly = [](const fs::path& file) {
        std::error_code e;
        fs::permissions(file, fs::perms::owner_write | fs::perms::group_write | fs::perms::others_write,
                        fs::perm_options::remove, e);
    };
    // Put the pool's file at `target`: a hard link, or a copy where the file
    // system cannot link.
    const auto place = [&](const fs::path& source) {
        std::error_code e;
        fs::create_hard_link(source, target, e);
        if (e) {
            fs::copy_file(source, target, fs::copy_options::overwrite_existing, e);
            if (e) {
                throw Error("Could not store " + target.string() + ": " + e.message());
            }
            readOnly(target);
        }
    };

    const bool pooled = !entry.digest.empty();
    const fs::path pool = _settings.versionStore / "objects" / entry.digest;
    if (pooled && fs::exists(pool, ec)) {
        place(pool);
        return Materialized::Linked;
    }

    static std::atomic<unsigned> counter{0};
    fs::path temp = target;
    temp += ".clio-tmp-" + std::to_string(CLIO_GETPID()) + "-" + std::to_string(counter.fetch_add(1));
    _pool.runOrThrow("print", {"-q", "-o", temp.string(), depotPath(AssetIdentifier::fromRelativePath(entry.path)) +
                                                              "#" + std::to_string(entry.rev)});
    if (!fs::exists(temp, ec)) {
        throw Error(entry.path + "#" + std::to_string(entry.rev) +
                    " is not on the server any more (obliterated or archived)");
    }
    if (check) {
        const std::string actual = md5File(temp, entry.isText());
        if (actual != entry.digest) {
            fs::remove(temp, ec);
            throw Error(entry.path + "#" + std::to_string(entry.rev) + " has digest " + actual +
                        " on the server, but the manifest has " + entry.digest +
                        ". The file was changed or restored after the manifest was made");
        }
    }
    if (bytesFetched) {
        *bytesFetched += static_cast<std::int64_t>(fs::file_size(temp, ec));
    }
    readOnly(temp);
    if (!pooled) {
        fs::rename(temp, target, ec);
        if (ec) {
            fs::remove(temp, ec);
            if (!fs::exists(target)) {
                throw Error("Could not store " + target.string());
            }
        }
        return Materialized::Fetched;
    }
    fs::create_directories(pool.parent_path(), ec);
    fs::rename(temp, pool, ec);
    if (ec) {
        // Another thread stored the same content first.
        fs::remove(temp, ec);
        if (!fs::exists(pool)) {
            throw Error("Could not store " + pool.string());
        }
    }
    place(pool);
    return Materialized::Fetched;
}

ManifestFetchReport Workspace::fetchManifest(const Manifest& manifest, std::size_t threads,
                                             const ManifestProgress& progress) {
    std::vector<const ManifestEntry*> entries;
    for (const auto& [path, entry] : manifest.entries()) {
        entries.push_back(&entry);
    }
    ManifestFetchReport report;
    report.files = entries.size();

    std::atomic<std::size_t> next{0};
    std::atomic<std::size_t> done{0};
    std::mutex reportMutex;
    const auto worker = [&] {
        for (;;) {
            const std::size_t index = next.fetch_add(1);
            if (index >= entries.size()) {
                return;
            }
            const ManifestEntry& entry = *entries[index];
            std::int64_t bytes = 0;
            std::string failure;
            Materialized how = Materialized::Present;
            try {
                how = materialize(manifest, entry, /*verifyPresent=*/true, &bytes);
            } catch (const std::exception& e) {
                failure = entry.path + ": " + e.what();
            }
            std::lock_guard<std::mutex> lock(reportMutex);
            if (!failure.empty()) {
                report.failures.push_back(failure);
            } else {
                switch (how) {
                case Materialized::Present: ++report.present; break;
                case Materialized::Linked: ++report.linked; break;
                case Materialized::Fetched: ++report.fetched; break;
                }
                report.bytes += bytes;
            }
            if (progress) {
                progress(++done, entries.size(), entry.path);
            }
        }
    };

    const std::size_t count = std::max<std::size_t>(1, std::min(threads, entries.size()));
    std::vector<std::thread> workers;
    for (std::size_t i = 1; i < count; ++i) {
        workers.emplace_back(worker);
    }
    worker();
    for (auto& t : workers) {
        t.join();
    }
    std::sort(report.failures.begin(), report.failures.end());
    return report;
}

std::vector<std::string> Workspace::checkManifest(const Manifest& manifest) {
    std::vector<std::string> problems;
    std::vector<const ManifestEntry*> entries;
    for (const auto& [path, entry] : manifest.entries()) {
        entries.push_back(&entry);
    }
    constexpr std::size_t chunk = 500;
    for (std::size_t start = 0; start < entries.size(); start += chunk) {
        const std::size_t end = std::min(entries.size(), start + chunk);
        std::vector<std::string> args{"-Ol", "-T", "depotFile,headRev,digest"};
        std::map<std::string, const ManifestEntry*> byDepotFile;
        for (std::size_t i = start; i < end; ++i) {
            const ManifestEntry& entry = *entries[i];
            const auto id = AssetIdentifier::fromRelativePath(entry.path);
            args.push_back(depotPath(id) + "#" + std::to_string(entry.rev));
            byDepotFile[_settings.depotRoot + "/" + entry.path] = &entry;
        }
        const auto result = _pool.runOrThrow("fstat", args);
        std::set<std::string> seen;
        for (const auto& record : result.records) {
            const auto file = record.find("depotFile");
            if (file == record.end() || !byDepotFile.count(file->second)) {
                continue;
            }
            const ManifestEntry& entry = *byDepotFile[file->second];
            seen.insert(entry.path);
            const auto digest = record.find("digest");
            if (digest != record.end() && !entry.digest.empty()) {
                std::string actual = digest->second;
                for (char& c : actual) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
                if (actual != entry.digest) {
                    problems.push_back(entry.path + "#" + std::to_string(entry.rev) +
                                       " has a different digest on the server than in the manifest");
                }
            }
        }
        for (std::size_t i = start; i < end; ++i) {
            if (!seen.count(entries[i]->path)) {
                problems.push_back(entries[i]->path + "#" + std::to_string(entries[i]->rev) +
                                   " is not on the server any more (obliterated or archived)");
            }
        }
    }
    return problems;
}

FetchResult Workspace::fetchVersion(const AssetIdentifier& id, const Pin& pin) {
    namespace fs = std::filesystem;
    const fs::path target = versionStorePath(id, pin);
    std::error_code ec;
    fs::create_directories(target.parent_path(), ec);
    if (ec) {
        throw Error("Could not create " + target.parent_path().string() + ": " + ec.message());
    }

    static std::atomic<unsigned> counter{0};
    fs::path temp = target;
    // Unique across threads and processes sharing the version store.
    temp += ".clio-tmp-" + std::to_string(CLIO_GETPID()) + "-" + std::to_string(counter.fetch_add(1));

    // A file missing at this version is a warning, not an error, so it
    // does not throw; connection and login failures do.
    _pool.runOrThrow("print", {"-q", "-o", temp.string(), depotPath(id) + pin.p4RevSpec()});

    if (!fs::exists(temp)) {
        return FetchResult::NotFound;
    }
    const auto writable = fs::perms::owner_write | fs::perms::group_write | fs::perms::others_write;
    fs::permissions(temp, writable, fs::perm_options::remove, ec); // stored versions are read-only
    if (fs::exists(target)) {
        // Replacing a stored label copy: some platforms refuse to replace a
        // read-only file.
        fs::permissions(target, fs::perms::owner_write, fs::perm_options::add, ec);
    }
    fs::rename(temp, target, ec);
    if (ec) {
        fs::remove(temp, ec);
        if (fs::exists(target)) {
            // Another process stored it first, or the old copy is in use
            // (Windows). For a change or revision the content is identical;
            // for a label the caller warns that the copy may be out of date.
            fs::permissions(target, writable, fs::perm_options::remove, ec);
            return FetchResult::KeptExisting;
        }
        throw Error("Could not store " + target.string());
    }
    return FetchResult::Fetched;
}

p4::CommandResult Workspace::run(const std::string& command,
                                 const std::vector<std::string>& args,
                                 const std::optional<std::string>& input) {
    return _pool.run(command, args, input);
}

} // namespace clio::core
