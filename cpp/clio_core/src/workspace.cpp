#include "clio/core/workspace.hpp"

#include "clio/core/error.hpp"

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

std::string pinDirName(const Pin& pin) {
    switch (pin.kind()) {
    case Pin::Kind::Change:
        return "change-" + std::to_string(pin.number());
    case Pin::Kind::Label:
        return "label-" + safeDirName(pin.labelName());
    case Pin::Kind::Revision:
        return "rev-" + std::to_string(pin.number());
    case Pin::Kind::Latest:
    case Pin::Kind::Have:
        break;
    }
    throw Error("Pin '" + pin.str() + "' is not a historical version");
}

} // namespace

Workspace::Workspace(Settings settings)
    : _settings(std::move(settings)), _connection(_settings.connection) {}

std::string Workspace::depotPath(const AssetIdentifier& id) const {
    return _settings.depotRoot + id.path();
}

std::filesystem::path Workspace::workspacePath(const AssetIdentifier& id) const {
    return _settings.workspaceRoot / std::filesystem::path(id.relativePath());
}

std::filesystem::path Workspace::versionStorePath(const AssetIdentifier& id, const Pin& pin) const {
    const std::string server = safeDirName(_settings.connection.port);
    const std::string depot = safeDirName(_settings.depotRoot.substr(2));
    return _settings.versionStore / server / depot / pinDirName(pin) /
           std::filesystem::path(id.relativePath());
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
    return _connection.runOrThrow("sync", args);
}

bool Workspace::fetchVersion(const AssetIdentifier& id, const Pin& pin) {
    const std::filesystem::path target = versionStorePath(id, pin);
    std::error_code ec;
    std::filesystem::create_directories(target.parent_path(), ec);
    if (ec) {
        throw Error("Could not create " + target.parent_path().string() + ": " + ec.message());
    }

    static std::atomic<unsigned> counter{0};
    std::filesystem::path temp = target;
    // Unique across threads and processes sharing the version store.
    temp += ".clio-tmp-" + std::to_string(CLIO_GETPID()) + "-" + std::to_string(counter.fetch_add(1));

    {
        std::lock_guard<std::mutex> lock(_mutex);
        _connection.runOrThrow("print", {"-q", "-o", temp.string(), depotPath(id) + pin.p4RevSpec()});
    }

    if (!std::filesystem::exists(temp)) {
        return false; // no such file at that version
    }
    std::filesystem::rename(temp, target, ec);
    if (ec) {
        std::filesystem::remove(temp, ec);
        // Another thread or process may have stored the same version first.
        if (std::filesystem::exists(target)) {
            return true;
        }
        throw Error("Could not store " + target.string());
    }
    return true;
}

p4::CommandResult Workspace::run(const std::string& command,
                                 const std::vector<std::string>& args,
                                 const std::optional<std::string>& input) {
    std::lock_guard<std::mutex> lock(_mutex);
    return _connection.run(command, args, input);
}

} // namespace clio::core
