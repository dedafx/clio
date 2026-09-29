#pragma once

#include <pxr/pxr.h>
#include <pxr/usd/ar/defineResolverContext.h>

#include <string>

PXR_NAMESPACE_OPEN_SCOPE

/// Resolver context for clio: paths (design doc §10.3).
///
/// Holds a clio settings string in canonical form, for example
///   depot=//imagine/main;root=/work/imagine;client=sam_ws;pin=@18234
/// Create one with ArGetResolver().CreateContextFromString("clio", text),
/// or set $CLIO_RESOLVER_CONTEXT to provide the default context.
class ClioResolverContext {
public:
    /// An empty context: clio: paths do not resolve while it is bound.
    ClioResolverContext() = default;

    /// Parse and validate `settings`. On error the context is empty and
    /// `*error` (if given) receives the reason.
    explicit ClioResolverContext(const std::string& settings, std::string* error = nullptr);

    bool IsEmpty() const { return _settings.empty(); }

    /// Canonical settings string, or "" for an empty context.
    const std::string& GetSettings() const { return _settings; }

    bool operator<(const ClioResolverContext& rhs) const { return _settings < rhs._settings; }
    bool operator==(const ClioResolverContext& rhs) const { return _settings == rhs._settings; }
    bool operator!=(const ClioResolverContext& rhs) const { return _settings != rhs._settings; }

private:
    std::string _settings;
};

size_t hash_value(const ClioResolverContext& context);

inline std::string ArGetDebugString(const ClioResolverContext& context) {
    return "ClioResolverContext('" + context.GetSettings() + "')";
}

AR_DECLARE_RESOLVER_CONTEXT(ClioResolverContext);

PXR_NAMESPACE_CLOSE_SCOPE
