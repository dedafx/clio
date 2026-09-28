#pragma once

#include <chrono>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace clio::core::p4 {

/// Settings for one Perforce connection. Empty values fall back to the
/// standard Perforce environment (P4PORT, P4USER, P4CLIENT, P4CONFIG,
/// P4ENVIRO, P4TICKETS), exactly as the p4 command line does.
struct ConnectionOptions {
    std::string port;
    std::string user;
    std::string client;
    std::string cwd;
    std::string programName = "clio";

    /// Ticket file to use instead of the default (P4TICKETS or ~/.p4tickets).
    std::string ticketFile;

    /// Answers Perforce prompts (for example the password asked by `login`).
    /// Returns nullopt to refuse. When unset, Clio never prompts: it runs on
    /// USD worker threads and in processes without a terminal (design doc
    /// §10.3), so a prompt there would hang.
    std::function<std::optional<std::string>(const std::string& prompt, bool noEcho)> prompt;

    /// Give up on an unreachable server after this long. The operating
    /// system's own TCP connect timeout can be over two minutes, and
    /// Perforce has no setting for it, so Clio checks reachability itself
    /// first (tcp and ssl ports). Zero disables the check.
    std::chrono::seconds connectTimeout{10};

    /// A command is cancelled if it runs longer than this. Zero disables the
    /// limit. See design doc §10.3 ("Blocking and timeouts").
    std::chrono::seconds commandTimeout{120};
};

/// One tagged output record, for example one file of `p4 fstat`.
using Record = std::map<std::string, std::string>;

/// A message reported by Perforce that was not tagged output.
struct Message {
    enum class Severity { Info = 1, Warning = 2, Failed = 3, Fatal = 4 };
    Severity severity = Severity::Info;
    std::string text;
};

/// Everything a command reported.
struct CommandResult {
    std::vector<Record> records;
    std::vector<Message> messages;

    /// True if any message is Failed or Fatal.
    bool hasErrors() const;

    /// Messages of Failed severity or worse, joined with newlines.
    std::string errorText() const;
};

/// A connection to a Perforce server through the P4 C++ API.
///
/// Not thread-safe: use one Connection per thread (design doc §8.1). The
/// connection opens on first use and stays open for later commands.
class Connection {
public:
    explicit Connection(ConnectionOptions options = {});
    ~Connection();

    Connection(const Connection&) = delete;
    Connection& operator=(const Connection&) = delete;
    Connection(Connection&&) noexcept;
    Connection& operator=(Connection&&) noexcept;

    /// Run `p4 <command> <args...>` with tagged output. `input` is sent to
    /// commands that read a spec from stdin (for example `client -i`).
    ///
    /// Throws P4Error if the connection fails or the server drops it.
    /// Server-reported command errors are returned in the result; call
    /// runOrThrow() to turn them into exceptions.
    CommandResult run(const std::string& command,
                      const std::vector<std::string>& args = {},
                      const std::optional<std::string>& input = std::nullopt);

    /// Like run(), but throws P4Error if the command reported errors.
    CommandResult runOrThrow(const std::string& command,
                             const std::vector<std::string>& args = {},
                             const std::optional<std::string>& input = std::nullopt);

    bool isConnected() const;
    void disconnect();

    const ConnectionOptions& options() const { return _options; }

private:
    struct Impl;

    void _connect();

    ConnectionOptions _options;
    std::unique_ptr<Impl> _impl;
};

} // namespace clio::core::p4
