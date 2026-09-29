#include "clio/core/settings.hpp"

#include "clio/core/error.hpp"

#include <charconv>
#include <cstdlib>
#include <map>

namespace clio::core {

namespace {

std::string trim(const std::string& s) {
    const auto first = s.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) {
        return {};
    }
    const auto last = s.find_last_not_of(" \t\r\n");
    return s.substr(first, last - first + 1);
}

std::string getEnv(const char* name) {
    const char* value = std::getenv(name);
    return value ? std::string(value) : std::string();
}

void checkValue(const std::string& key, const std::string& value) {
    if (value.find_first_of(";=") != std::string::npos) {
        throw ConfigError("Setting '" + key + "' may not contain ';' or '='");
    }
}

void validateDepotRoot(const std::string& depot) {
    if (depot.size() < 3 || depot.rfind("//", 0) != 0) {
        throw ConfigError("depot must be a depot path such as //imagine/main, got '" + depot + "'");
    }
    if (depot.back() == '/') {
        throw ConfigError("depot must not end with '/', got '" + depot + "'");
    }
    if (depot.find_first_of("@#%*") != std::string::npos || depot.find("...") != std::string::npos) {
        throw ConfigError("depot must not contain wildcards or revision specifiers, got '" + depot + "'");
    }
}

// A whole, non-negative number of seconds. Rejects trailing text such as
// "10seconds", which std::stol would accept as 10.
std::chrono::seconds parseSeconds(const std::string& key, const std::string& value) {
    long long seconds = -1;
    const char* first = value.data();
    const char* last = value.data() + value.size();
    const auto [ptr, ec] = std::from_chars(first, last, seconds);
    if (value.empty() || ec != std::errc() || ptr != last || seconds < 0) {
        throw ConfigError(key + " must be a whole number of seconds, got '" + value + "'");
    }
    return std::chrono::seconds(seconds);
}

} // namespace

std::string policyName(Policy policy) {
    switch (policy) {
    case Policy::Sync:
        return "sync";
    case Policy::Verify:
        return "verify";
    case Policy::Offline:
        return "offline";
    }
    return "sync";
}

Policy parsePolicy(const std::string& text) {
    if (text == "sync") return Policy::Sync;
    if (text == "verify") return Policy::Verify;
    if (text == "offline") return Policy::Offline;
    throw ConfigError("Unknown policy '" + text + "': expected sync, verify or offline");
}

std::filesystem::path Settings::defaultCacheDir() {
#ifdef _WIN32
    if (auto local = getEnv("LOCALAPPDATA"); !local.empty()) {
        return std::filesystem::path(local) / "clio";
    }
#endif
    if (auto xdg = getEnv("XDG_CACHE_HOME"); !xdg.empty()) {
        return std::filesystem::path(xdg) / "clio";
    }
    if (auto home = getEnv("HOME"); !home.empty()) {
        return std::filesystem::path(home) / ".cache" / "clio";
    }
    return std::filesystem::temp_directory_path() / "clio";
}

std::filesystem::path Settings::defaultVersionStore() {
    if (auto explicitStore = getEnv("CLIO_VERSION_STORE"); !explicitStore.empty()) {
        return explicitStore;
    }
    return defaultCacheDir() / "versions";
}

Settings Settings::parse(const std::string& text) {
    std::map<std::string, std::string> values;
    std::size_t start = 0;
    while (start <= text.size()) {
        std::size_t end = text.find(';', start);
        if (end == std::string::npos) {
            end = text.size();
        }
        const std::string item = trim(text.substr(start, end - start));
        start = end + 1;
        if (item.empty()) {
            continue;
        }
        const std::size_t eq = item.find('=');
        if (eq == std::string::npos) {
            throw ConfigError("Invalid setting '" + item + "': expected key=value");
        }
        const std::string key = trim(item.substr(0, eq));
        const std::string value = trim(item.substr(eq + 1));
        if (!values.emplace(key, value).second) {
            throw ConfigError("Setting '" + key + "' is given more than once");
        }
    }

    Settings settings;
    for (const auto& [key, value] : values) {
        if (key == "depot") {
            validateDepotRoot(value);
            settings.depotRoot = value;
        } else if (key == "root") {
            settings.workspaceRoot = value;
        } else if (key == "store") {
            settings.versionStore = value;
        } else if (key == "port") {
            settings.connection.port = value;
        } else if (key == "user") {
            settings.connection.user = value;
        } else if (key == "client") {
            settings.connection.client = value;
        } else if (key == "tickets") {
            settings.connection.ticketFile = value;
        } else if (key == "pin") {
            settings.pin = Pin::parse(value);
            if (settings.pin.kind() == Pin::Kind::Revision) {
                throw ConfigError("A revision pin (#N) applies to one file and cannot be a context pin");
            }
        } else if (key == "policy") {
            settings.policy = parsePolicy(value);
        } else if (key == "connect_timeout") {
            settings.connection.connectTimeout = parseSeconds(key, value);
        } else if (key == "timeout") {
            settings.connection.commandTimeout = parseSeconds(key, value);
        } else {
            throw ConfigError("Unknown setting '" + key + "'");
        }
    }

    if (settings.depotRoot.empty()) {
        throw ConfigError("Missing required setting 'depot' (for example depot=//imagine/main)");
    }
    if (settings.workspaceRoot.empty()) {
        throw ConfigError("Missing required setting 'root' (the local workspace directory)");
    }
    if (settings.versionStore.empty()) {
        settings.versionStore = defaultVersionStore();
    }
    return settings;
}

std::string Settings::str() const {
    std::string out;
    auto add = [&out](const std::string& key, const std::string& value) {
        checkValue(key, value);
        if (!out.empty()) {
            out += ';';
        }
        out += key + "=" + value;
    };
    add("depot", depotRoot);
    add("root", workspaceRoot.generic_string());
    add("store", versionStore.generic_string());
    if (!connection.port.empty()) add("port", connection.port);
    if (!connection.user.empty()) add("user", connection.user);
    if (!connection.client.empty()) add("client", connection.client);
    if (!connection.ticketFile.empty()) add("tickets", connection.ticketFile);
    if (pin != Pin::have()) add("pin", pin.str());
    if (policy != Policy::Sync) add("policy", policyName(policy));
    if (connection.connectTimeout != p4::ConnectionOptions{}.connectTimeout) {
        add("connect_timeout", std::to_string(connection.connectTimeout.count()));
    }
    if (connection.commandTimeout != p4::ConnectionOptions{}.commandTimeout) {
        add("timeout", std::to_string(connection.commandTimeout.count()));
    }
    return out;
}

} // namespace clio::core
