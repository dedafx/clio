#pragma once

#include <stdexcept>
#include <string>
#include <vector>

namespace clio::core {

/// Base class for every error raised by clio_core.
class Error : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

/// A string could not be parsed as a clio: asset identifier.
class IdentifierError : public Error {
public:
    using Error::Error;
};

/// A string could not be parsed as a version pin.
class PinError : public Error {
public:
    using Error::Error;
};

/// Configuration is missing or invalid.
class ConfigError : public Error {
public:
    using Error::Error;
};

/// The Perforce server or client API reported an error.
class P4Error : public Error {
public:
    P4Error(const std::string& message, std::vector<std::string> p4Messages = {})
        : Error(message), _p4Messages(std::move(p4Messages)) {}

    /// The raw messages reported by Perforce, for debugging.
    const std::vector<std::string>& p4Messages() const { return _p4Messages; }

private:
    std::vector<std::string> _p4Messages;
};

} // namespace clio::core
