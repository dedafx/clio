#include "clio/core/p4/connection.hpp"

#include "clio/core/error.hpp"
#include "clio/core/version.hpp"

#include <clientapi.h>
#include <p4libs.h> // clientapi.h already includes keepalive.h, which has no include guard

#include <mutex>

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

    ::Error e;
    client.Init(&e);
    if (e.Test()) {
        throw P4Error("Could not connect to Perforce: " + formatError(e), {formatError(e)});
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
