#include "clio/core/workspace.hpp"

#include "clio/core/error.hpp"
#include "clio/core/file_lock.hpp"

#include <cstdint>
#include <cstdio>

#include <atomic>
#include <system_error>

#ifdef _WIN32
#include <process.h>
#define CLIO_GETPID _getpid
#else
#include <unistd.h>
#define CLIO_GETPID getpid
#endif

namespace clio::core {

namespace {

// Make a string safe to use as one directory name.
std::string safeDirName(const std::string& text) {
    std::string out;
    out.reserve(text.size());
    for (unsigned char c : text) {
        const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                        (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.';
        out += ok ? static_cast<char>(c) : '_';
    }
    return out.empty() ? std::string("default") : out;
}

// Reversible, collision-free folder name for any label: bytes outside
// [A-Za-z0-9_.-] become %XX. ("approved:prod" -> "approved%3Aprod", while
// "approved_prod" stays as it is.)
std::string encodeLabel(const std::string& label) {
    static const char* hex = "0123456789ABCDEF";
    std::string out;
    for (unsigned char c : label) {
        const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                        (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.';
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
    return encodeLabel(out) == encoded ? std::optional<std::string>(out) : std::nullopt;
}

std::string pinDirName(const Pin& pin) {
    switch (pin.kind()) {
    case Pin::Kind::Change:
        return "change-" + std::to_string(pin.number());
    case Pin::Kind::Label:
        return "label-" + encodeLabel(pin.labelName());
    case Pin::Kind::Revision:
        return "rev-" + std::to_string(pin.number());
    case Pin::Kind::Latest:
    case Pin::Kind::Have:
        break;
    }
    throw Error("Pin '" + pin.str() + "' is not a historical version");
}

// `path` relative to `base` if it is inside it, using '/' separators.
std::optional<std::string> relativeInside(const std::filesystem::path& path,
                                          const std::filesystem::path& base) {
    if (base.empty() || !path.is_absolute()) {
        return std::nullopt;
    }
    const auto rel = path.lexically_normal().lexically_relative(base.lexically_normal());
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

} // namespace

Workspace::Workspace(Settings settings)
    : _settings(std::move(settings)), _connection(_settings.connection) {
    // Name the server's store folder by the port actually used, so different
    // servers taken from the environment never share a folder. (Equivalent
    // aliases for one server get separate folders, which is safe.)
    std::string port = _settings.connection.port;
    if (port.empty()) {
        try {
            port = _connection.effectivePort();
        } catch (const Error&) {
        }
    }
    _serverKey = safeDirName(port);
}

std::filesystem::path Workspace::lockPath() const {
    // Stable 64-bit FNV-1a hash, the same in every build of clio_core.
    std::uint64_t hash = 1469598103934665603ull;
    const std::string key = _serverKey + "\n" + _settings.connection.client + "\n" +
                            _settings.workspaceRoot.lexically_normal().generic_string();
    for (unsigned char c : key) {
        hash = (hash ^ c) * 1099511628211ull;
    }
    char name[32];
    std::snprintf(name, sizeof(name), "%016llx.lock", static_cast<unsigned long long>(hash));
    return Settings::defaultCacheDir() / "locks" / name;
}

std::string Workspace::depotPath(const AssetIdentifier& id) const {
    return _settings.depotRoot + id.path();
}

std::filesystem::path Workspace::workspacePath(const AssetIdentifier& id) const {
    return _settings.workspaceRoot / std::filesystem::path(id.relativePath());
}

std::filesystem::path Workspace::versionStorePath(const AssetIdentifier& id, const Pin& pin) const {
    const std::string depot = safeDirName(_settings.depotRoot.substr(2));
    return _settings.versionStore / _serverKey / depot / pinDirName(pin) /
           std::filesystem::path(id.relativePath());
}

std::optional<LocalPathMatch> Workspace::matchLocalPath(const std::filesystem::path& path) const {
    const auto storeBase = _settings.versionStore / _serverKey /
                           safeDirName(_settings.depotRoot.substr(2));
    if (auto rel = relativeInside(path, storeBase)) {
        const std::size_t slash = rel->find('/');
        if (slash == std::string::npos) {
            return std::nullopt;
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

p4::CommandResult Workspace::sync(const std::vector<AssetIdentifier>& ids, const Pin& pin) {
    if (pin.kind() == Pin::Kind::Revision) {
        throw Error("A revision pin is fetched into the version store, not synced");
    }
    std::vector<std::string> args{"-q"};
    args.reserve(ids.size() + 1);
    for (const auto& id : ids) {
        args.push_back(depotPath(id) + pin.p4RevSpec());
    }
    std::lock_guard<std::mutex> lock(_mutex);
    // Other processes (a DCC's resolver, the clio CLI) may change the same
    // workspace; serialize with them too.
    FileLock workspaceLock(lockPath());
    return _connection.runOrThrow("sync", args);
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

    {
        std::lock_guard<std::mutex> lock(_mutex);
        // A file missing at this version is a warning, not an error, so it
        // does not throw; connection and login failures do.
        _connection.runOrThrow("print", {"-q", "-o", temp.string(), depotPath(id) + pin.p4RevSpec()});
    }

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
    std::lock_guard<std::mutex> lock(_mutex);
    return _connection.run(command, args, input);
}

} // namespace clio::core
