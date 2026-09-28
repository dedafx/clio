#include "clio/core/p4/connection.hpp"

#include "clio/core/error.hpp"
#include "clio/core/version.hpp"

#include <clientapi.h>
#include <p4libs.h> // clientapi.h already includes keepalive.h, which has no include guard

#include <cerrno>
#include <cstring>
#include <mutex>

#ifndef _WIN32
#include <fcntl.h>
#include <netdb.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

namespace clio::core::p4 {

namespace {

// Inside CollectingUser the name Message refers to ClientUser::Message().
using ClioMessage = clio::core::p4::Message;

std::string formatError(const ::Error& err) {
    StrBuf buf;
    err.Fmt(buf, EF_PLAIN);
    std::string text(buf.Text(), buf.Length());
    while (!text.empty() && (text.back() == '\n' || text.back() == '\r')) {
        text.pop_back();
    }
    return text;
}

// The P4 libraries must be initialized once per process (per copy of the
// static libraries) before any ClientApi is used. They are never shut down:
// the process owns them until exit.
void initializeP4Libraries() {
    static std::once_flag once;
    static std::string failure;
    std::call_once(once, [] {
        ::Error e;
        P4Libraries::Initialize(P4LIBRARIES_INIT_ALL, &e);
        if (e.Test()) {
            failure = formatError(e);
        }
    });
    if (!failure.empty()) {
        throw P4Error("Could not initialize the Perforce API: " + failure);
    }
}

// Split a P4PORT such as "ssl:perforce:1666", "perforce:1666", "1666" or
// "tcp6:[::1]:1666" into host and service. Returns false for ports that do
// not use TCP directly (rsh:, jsh:), which are not checked.
bool splitTcpPort(std::string port, std::string& host, std::string& service) {
    if (port.rfind("rsh:", 0) == 0 || port.rfind("jsh:", 0) == 0) {
        return false;
    }
    for (const char* proto : {"tcp:", "tcp4:", "tcp6:", "tcp46:", "tcp64:",
                              "ssl:", "ssl4:", "ssl6:", "ssl46:", "ssl64:"}) {
        if (port.rfind(proto, 0) == 0) {
            port.erase(0, std::strlen(proto));
            break;
        }
    }
    const std::size_t colon = port.rfind(':');
    if (colon == std::string::npos) {
        host = "localhost";
        service = port;
    } else {
        host = port.substr(0, colon);
        service = port.substr(colon + 1);
    }
    if (host.size() > 1 && host.front() == '[' && host.back() == ']') {
        host = host.substr(1, host.size() - 2);
    }
    return !host.empty() && !service.empty();
}

// Try a TCP connection with a time limit. Returns an error message, or ""
// if the server accepted the connection.
std::string checkReachable(const std::string& port, std::chrono::seconds timeout) {
#ifdef _WIN32
    // TODO: Windows implementation (Winsock). Until then, rely on P4API.
    (void)port;
    (void)timeout;
    return {};
#else
    std::string host, service;
    if (timeout.count() <= 0 || !splitTcpPort(port, host, service)) {
        return {};
    }
    addrinfo hints{};
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* addresses = nullptr;
    if (const int rc = getaddrinfo(host.c_str(), service.c_str(), &hints, &addresses); rc != 0) {
        return "cannot resolve " + host + ": " + gai_strerror(rc);
    }
    std::string failure = "cannot reach " + host + ":" + service;
    const int timeoutMs = static_cast<int>(timeout.count() * 1000);
    for (addrinfo* ai = addresses; ai; ai = ai->ai_next) {
        const int fd = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (fd < 0) {
            continue;
        }
        fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
        int rc = connect(fd, ai->ai_addr, ai->ai_addrlen);
        if (rc != 0 && errno == EINPROGRESS) {
            pollfd pfd{fd, POLLOUT, 0};
            rc = poll(&pfd, 1, timeoutMs);
            if (rc == 1) {
                int soError = 0;
                socklen_t len = sizeof(soError);
                getsockopt(fd, SOL_SOCKET, SO_ERROR, &soError, &len);
                rc = soError == 0 ? 0 : -1;
                if (soError != 0) {
                    failure = "cannot reach " + host + ":" + service + ": " + std::strerror(soError);
                }
            } else {
                failure = "no answer from " + host + ":" + service + " within " +
                          std::to_string(timeout.count()) + " s";
                rc = -1;
            }
        } else if (rc != 0) {
            failure = "cannot reach " + host + ":" + service + ": " + std::strerror(errno);
        }
        close(fd);
        if (rc == 0) {
            freeaddrinfo(addresses);
            return {};
        }
    }
    freeaddrinfo(addresses);
    return failure;
#endif
}

// Collects everything a command reports. Never prompts: Clio runs in
// processes without a terminal and on USD worker threads (design doc §10.3).
class CollectingUser : public ClientUser {
public:
    CommandResult result;
    std::optional<std::string> input;
    const ConnectionOptions* options = nullptr;

    void OutputStat(StrDict* dict) override {
        Record record;
        StrRef var, val;
        for (int i = 0; dict->GetVar(i, var, val); ++i) {
            if (var == "func" || var == "specFormatted") {
                continue;
            }
            record.emplace(std::string(var.Text(), var.Length()),
                           std::string(val.Text(), val.Length()));
        }
        result.records.push_back(std::move(record));
    }

    void OutputInfo(char /*level*/, const char* data) override {
        result.messages.push_back({ClioMessage::Severity::Info, data ? data : ""});
    }

    void OutputText(const char* /*data*/, int /*length*/) override {}
    void OutputBinary(const char* /*data*/, int /*length*/) override {}

    void Message(::Error* err) override { record(*err); }
    void HandleError(::Error* err) override { record(*err); }

    void OutputError(const char* errBuf) override {
        result.messages.push_back({ClioMessage::Severity::Failed, errBuf ? errBuf : ""});
    }

    void InputData(StrBuf* buf, ::Error* e) override {
        if (!input) {
            e->Set(E_FAILED, "Clio: command asked for input but none was given");
            return;
        }
        buf->Set(input->c_str());
    }

    void Prompt(const StrPtr& msg, StrBuf& rsp, int noEcho, ::Error* e) override {
        answer(std::string(msg.Text(), msg.Length()), rsp, noEcho != 0, e);
    }
    void Prompt(const StrPtr& msg, StrBuf& rsp, int noEcho, int /*noOutput*/, ::Error* e) override {
        answer(std::string(msg.Text(), msg.Length()), rsp, noEcho != 0, e);
    }
    void Prompt(::Error* err, StrBuf& rsp, int noEcho, ::Error* e) override {
        answer(formatError(*err), rsp, noEcho != 0, e);
    }
    void Prompt(::Error* err, StrBuf& rsp, int noEcho, int /*noOutput*/, ::Error* e) override {
        answer(formatError(*err), rsp, noEcho != 0, e);
    }

    int CanAutoLoginPrompt() override { return 0; }

private:
    void record(const ::Error& err) {
        const auto severity = err.GetSeverity();
        if (severity == E_EMPTY) {
            return;
        }
        result.messages.push_back(
            {static_cast<ClioMessage::Severity>(severity), formatError(err)});
    }

    void answer(const std::string& prompt, StrBuf& rsp, bool noEcho, ::Error* e) {
        if (options && options->prompt) {
            if (auto reply = options->prompt(prompt, noEcho)) {
                rsp.Set(reply->c_str());
                return;
            }
        }
        e->Set(E_FAILED, "Clio does not prompt for passwords. Run 'clio login' (or 'p4 login').");
    }
};

// Cancels a running command once its deadline has passed.
class DeadlineKeepAlive : public KeepAlive {
public:
    void arm(std::chrono::seconds timeout) {
        _enabled = timeout.count() > 0;
        _deadline = std::chrono::steady_clock::now() + timeout;
        _expired = false;
    }

    int IsAlive() override {
        if (_enabled && std::chrono::steady_clock::now() > _deadline) {
            _expired = true;
            return 0;
        }
        return 1;
    }

    bool expired() const { return _expired; }

private:
    bool _enabled = false;
    bool _expired = false;
    std::chrono::steady_clock::time_point _deadline;
};

} // namespace

bool CommandResult::hasErrors() const {
    for (const auto& m : messages) {
        if (m.severity >= Message::Severity::Failed) {
            return true;
        }
    }
    return false;
}

std::string CommandResult::errorText() const {
    std::string text;
    for (const auto& m : messages) {
        if (m.severity >= Message::Severity::Failed) {
            if (!text.empty()) {
                text += '\n';
            }
            text += m.text;
        }
    }
    return text;
}

struct Connection::Impl {
    ClientApi client;
    DeadlineKeepAlive keepAlive;
    bool connected = false;
};

Connection::Connection(ConnectionOptions options)
    : _options(std::move(options)), _impl(std::make_unique<Impl>()) {}

Connection::~Connection() {
    try {
        disconnect();
    } catch (...) {
        // Never throw from a destructor.
    }
}

Connection::Connection(Connection&&) noexcept = default;
Connection& Connection::operator=(Connection&&) noexcept = default;

bool Connection::isConnected() const {
    return _impl && _impl->connected && !_impl->client.Dropped();
}

void Connection::disconnect() {
    if (_impl && _impl->connected) {
        ::Error e;
        _impl->client.Final(&e);
        _impl->connected = false;
    }
}

std::string Connection::effectivePort() const {
    if (!_options.port.empty()) {
        return _options.port;
    }
    initializeP4Libraries();
    ClientApi client; // reads the environment; does not connect
    if (!_options.cwd.empty()) client.SetCwd(_options.cwd.c_str());
    const StrPtr& port = client.GetPort();
    return std::string(port.Text(), port.Length());
}

void Connection::_connect() {
    initializeP4Libraries();

    if (!_impl) { // moved-from
        _impl = std::make_unique<Impl>();
    }

    if (_impl->connected && _impl->client.Dropped()) {
        disconnect();
        _impl = std::make_unique<Impl>();
    }
    if (_impl->connected) {
        return;
    }

    ClientApi& client = _impl->client;
    client.SetProtocol("tag", "");
    if (!_options.port.empty()) client.SetPort(_options.port.c_str());
    if (!_options.user.empty()) client.SetUser(_options.user.c_str());
    if (!_options.client.empty()) client.SetClient(_options.client.c_str());
    if (!_options.cwd.empty()) client.SetCwd(_options.cwd.c_str());
    if (!_options.ticketFile.empty()) client.SetTicketFile(_options.ticketFile.c_str());

    // P4PORT may come from the environment or P4CONFIG, so ask the API.
    const StrPtr& effectivePort = client.GetPort();
    const std::string port(effectivePort.Text(), effectivePort.Length());
    if (const std::string unreachable = checkReachable(port, _options.connectTimeout);
        !unreachable.empty()) {
        _impl = std::make_unique<Impl>();
        throw P4Error("Could not connect to Perforce at " + port + ": " + unreachable);
    }

    ::Error e;
    client.Init(&e);
    if (e.Test()) {
        const std::string detail = formatError(e);
        _impl = std::make_unique<Impl>(); // start clean on the next attempt
        throw P4Error("Could not connect to Perforce: " + detail, {detail});
    }
    // Must follow Init() (see clientapi.h).
    client.SetProg(_options.programName.c_str());
    client.SetVersion(CLIO_VERSION_STRING);
    client.SetBreak(&_impl->keepAlive);
    _impl->connected = true;
}

CommandResult Connection::run(const std::string& command,
                              const std::vector<std::string>& args,
                              const std::optional<std::string>& input) {
    _connect();

    std::vector<char*> argv;
    argv.reserve(args.size());
    for (const auto& arg : args) {
        argv.push_back(const_cast<char*>(arg.c_str()));
    }

    CollectingUser ui;
    ui.input = input;
    ui.options = &_options;
    _impl->keepAlive.arm(_options.commandTimeout);
    _impl->client.SetArgv(static_cast<int>(argv.size()), argv.data());
    _impl->client.Run(command.c_str(), &ui);

    if (_impl->keepAlive.expired()) {
        disconnect();
        throw P4Error("Perforce command '" + command + "' timed out after " +
                      std::to_string(_options.commandTimeout.count()) + " s");
    }
    if (_impl->client.Dropped()) {
        const std::string detail = ui.result.errorText();
        disconnect();
        throw P4Error("Connection to Perforce was lost during '" + command + "'" +
                          (detail.empty() ? std::string() : ": " + detail),
                      {detail});
    }
    return std::move(ui.result);
}

CommandResult Connection::runOrThrow(const std::string& command,
                                     const std::vector<std::string>& args,
                                     const std::optional<std::string>& input) {
    CommandResult result = run(command, args, input);
    if (result.hasErrors()) {
        std::vector<std::string> raw;
        for (const auto& m : result.messages) {
            raw.push_back(m.text);
        }
        throw P4Error("p4 " + command + " failed: " + result.errorText(), std::move(raw));
    }
    return result;
}

} // namespace clio::core::p4
