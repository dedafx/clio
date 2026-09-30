// deda.clio._core: Python bindings for clio_core.

#include "clio/core/error.hpp"
#include "clio/core/identifier.hpp"
#include "clio/core/p4/connection.hpp"
#include "clio/core/pin.hpp"
#include "clio/core/resolver.hpp"
#include "clio/core/settings.hpp"
#include "clio/core/version.hpp"

#include <nanobind/nanobind.h>
#include <nanobind/stl/filesystem.h>
#include <nanobind/stl/function.h>
#include <nanobind/stl/map.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>

namespace nb = nanobind;
using namespace nb::literals;
using namespace clio::core;

namespace {

nb::dict resolvedToDict(const ResolvedAsset& asset) {
    nb::dict d;
    d["identifier"] = asset.id.str();
    d["pin"] = asset.pin.str();
    d["depot_path"] = asset.depotPath;
    d["local_path"] = asset.localPath;
    d["warning"] = asset.warning.empty() ? nb::none() : nb::cast(asset.warning);
    return d;
}

} // namespace

NB_MODULE(_core, m) {
    m.doc() = "Clio's C++ core: Perforce access and clio: asset resolution.";
    m.attr("__version__") = version();

    // Exceptions. Specific classes derive from ClioError so callers can catch
    // either. nanobind tries translators in reverse order of registration, so
    // the base class is registered first and the specific ones after it.
    auto clioError = nb::exception<Error>(m, "ClioError");
    nb::exception<IdentifierError>(m, "IdentifierError", clioError);
    nb::exception<PinError>(m, "PinError", clioError);
    nb::exception<ConfigError>(m, "ConfigError", clioError);
    nb::exception<P4Error>(m, "P4Error", clioError);

    nb::enum_<Pin::Kind>(m, "PinKind")
        .value("LATEST", Pin::Kind::Latest)
        .value("HAVE", Pin::Kind::Have)
        .value("CHANGE", Pin::Kind::Change)
        .value("LABEL", Pin::Kind::Label)
        .value("REVISION", Pin::Kind::Revision);

    nb::class_<Pin>(m, "Pin", "Which version of an asset to use (design doc §10.4).")
        .def_static("parse", &Pin::parse, "text"_a,
                    "Parse 'latest', 'have', '@<change>', '@<label>' or '#<revision>'.")
        .def_static("latest", &Pin::latest)
        .def_static("have", &Pin::have)
        .def_static("change", &Pin::change, "change"_a)
        .def_static("label", &Pin::label, "label"_a)
        .def_static("revision", &Pin::revision, "revision"_a)
        .def_prop_ro("kind", &Pin::kind)
        .def_prop_ro("number", &Pin::number)
        .def_prop_ro("label_name", &Pin::labelName)
        .def_prop_ro("is_snapshot", &Pin::isSnapshot)
        .def_prop_ro("is_historical", &Pin::isHistorical)
        .def("p4_rev_spec", &Pin::p4RevSpec)
        .def("__str__", &Pin::str)
        .def("__repr__", [](const Pin& p) { return "Pin('" + p.str() + "')"; })
        .def("__eq__", [](const Pin& a, const Pin& b) { return a == b; })
        .def("__hash__", [](const Pin& p) { return nb::hash(nb::str(p.str().c_str())); });

    nb::class_<AssetIdentifier>(m, "AssetIdentifier",
                                "A clio: asset identifier, e.g. clio:/props/crate/crate.usd?change=42.")
        .def_static("parse", &AssetIdentifier::parse, "text"_a)
        .def_static("is_clio_uri", &AssetIdentifier::isClioUri, "text"_a)
        .def_static("anchor", &AssetIdentifier::anchor, "asset_path"_a, "anchor"_a,
                    "Anchor a relative path to another identifier, inheriting its snapshot pin.")
        .def_prop_ro("path", &AssetIdentifier::path)
        .def_prop_ro("relative_path", &AssetIdentifier::relativePath)
        .def_prop_ro("pin", &AssetIdentifier::pin)
        .def("__str__", &AssetIdentifier::str)
        .def("__repr__", [](const AssetIdentifier& i) { return "AssetIdentifier('" + i.str() + "')"; })
        .def("__eq__", [](const AssetIdentifier& a, const AssetIdentifier& b) { return a == b; })
        .def("__hash__", [](const AssetIdentifier& i) { return nb::hash(nb::str(i.str().c_str())); });

    nb::enum_<Policy>(m, "Policy")
        .value("SYNC", Policy::Sync)
        .value("VERIFY", Policy::Verify)
        .value("OFFLINE", Policy::Offline);

    nb::class_<Settings>(m, "Settings",
                         "Project settings; also the text form of a USD resolver context.")
        .def_static("parse", &Settings::parse, "text"_a)
        .def_prop_ro("depot_root", [](const Settings& s) { return s.depotRoot; })
        .def_prop_ro("workspace_root", [](const Settings& s) { return s.workspaceRoot; })
        .def_prop_ro("version_store", [](const Settings& s) { return s.versionStore; })
        .def_prop_ro("pin", [](const Settings& s) { return s.pin; })
        .def_prop_ro("policy", [](const Settings& s) { return s.policy; })
        .def_prop_ro("port", [](const Settings& s) { return s.connection.port; })
        .def_prop_ro("user", [](const Settings& s) { return s.connection.user; })
        .def_prop_ro("client", [](const Settings& s) { return s.connection.client; })
        .def("__str__", &Settings::str)
        .def("__repr__", [](const Settings& s) { return "Settings('" + s.str() + "')"; })
        .def("__eq__", [](const Settings& a, const Settings& b) { return a == b; });

    nb::class_<p4::Message>(m, "Message")
        .def_prop_ro("severity", [](const p4::Message& msg) { return static_cast<int>(msg.severity); })
        .def_prop_ro("text", [](const p4::Message& msg) { return msg.text; })
        .def("__repr__", [](const p4::Message& msg) { return "Message(" + msg.text + ")"; });

    nb::class_<p4::CommandResult>(m, "CommandResult")
        .def_prop_ro("records", [](const p4::CommandResult& r) { return r.records; })
        .def_prop_ro("messages", [](const p4::CommandResult& r) { return r.messages; })
        .def_prop_ro("has_errors", &p4::CommandResult::hasErrors)
        .def("error_text", &p4::CommandResult::errorText);

    // The GIL is released for every call that can touch the network or disk,
    // so Python threads (and UIs) keep running while Perforce works.
    nb::class_<p4::Connection>(m, "Connection",
                               "A Perforce connection. Not thread-safe: use one per thread.")
        .def(
            "__init__",
            [](p4::Connection* self, const std::string& port, const std::string& user,
               const std::string& client, const std::string& cwd, const std::string& tickets,
               int timeout, int connectTimeout, std::optional<nb::callable> prompt) {
                p4::ConnectionOptions options;
                options.port = port;
                options.user = user;
                options.client = client;
                options.cwd = cwd;
                options.ticketFile = tickets;
                options.commandTimeout = std::chrono::seconds(timeout);
                options.connectTimeout = std::chrono::seconds(connectTimeout);
                if (prompt) {
                    // Called from run() with the GIL released; take it back.
                    auto callback = std::make_shared<nb::callable>(std::move(*prompt));
                    options.prompt = [callback](const std::string& text,
                                                bool noEcho) -> std::optional<std::string> {
                        nb::gil_scoped_acquire acquire;
                        nb::object reply = (*callback)(text, noEcho);
                        if (reply.is_none()) {
                            return std::nullopt;
                        }
                        return nb::cast<std::string>(reply);
                    };
                }
                new (self) p4::Connection(std::move(options));
            },
            "port"_a = "", "user"_a = "", "client"_a = "", "cwd"_a = "", "tickets"_a = "",
            "timeout"_a = 120, "connect_timeout"_a = 10, "prompt"_a = nb::none(),
            "prompt(text, no_echo) -> str | None answers Perforce prompts such as the "
            "password for 'login'. Without it, Clio never prompts.")
        .def("run", &p4::Connection::run, "command"_a, "args"_a = std::vector<std::string>{},
             "input"_a = nb::none(), nb::call_guard<nb::gil_scoped_release>())
        .def("run_or_throw", &p4::Connection::runOrThrow, "command"_a,
             "args"_a = std::vector<std::string>{}, "input"_a = nb::none(),
             nb::call_guard<nb::gil_scoped_release>())
        .def_prop_ro("is_connected", &p4::Connection::isConnected)
        .def("disconnect", &p4::Connection::disconnect);

    nb::class_<AssetResolver>(m, "AssetResolver",
                              "Resolve clio: identifiers to local files (design doc §10.4). Thread-safe.")
        .def(nb::init<Settings>(), "settings"_a)
        .def_prop_ro("settings", &AssetResolver::settings)
        .def("effective_pin", &AssetResolver::effectivePin, "identifier"_a)
        .def(
            "resolve",
            [](AssetResolver& r, const AssetIdentifier& id) -> std::optional<nb::dict> {
                std::optional<ResolvedAsset> asset;
                {
                    nb::gil_scoped_release release;
                    asset = r.resolve(id);
                }
                if (!asset) {
                    return std::nullopt;
                }
                return resolvedToDict(*asset);
            },
            "identifier"_a,
            "Return {identifier, pin, depot_path, local_path, warning}, or None if the asset is not "
            "available. 'warning' is set when a local file was used instead of the exact version, "
            "for example because Perforce was unreachable.")
        .def(
            "resolve_path",
            [](AssetResolver& r, const std::filesystem::path& path) -> std::optional<nb::dict> {
                std::optional<ResolvedAsset> asset;
                {
                    nb::gil_scoped_release release;
                    asset = r.resolvePath(path);
                }
                if (!asset) {
                    return std::nullopt;
                }
                return resolvedToDict(*asset);
            },
            "path"_a,
            "Resolve a local path as USD would: a workspace path uses the settings pin, a "
            "version-store path the version its folder holds. None if not managed or not available.")
        .def("manages", &AssetResolver::manages, "path"_a,
             "True if the path is inside the workspace root or this project's version store.")
        .def("resolve_for_new_asset", &AssetResolver::resolveForNewAsset, "identifier"_a)
        .def("refresh", &AssetResolver::refresh);
}
