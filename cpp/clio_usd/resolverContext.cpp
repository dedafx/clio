#include "resolverContext.h"

#include "clio/core/error.hpp"
#include "clio/core/settings.hpp"

#include <functional>

PXR_NAMESPACE_OPEN_SCOPE

ClioResolverContext::ClioResolverContext(const std::string& settings, std::string* error) {
    try {
        _settings = clio::core::Settings::parse(settings).str();
    } catch (const clio::core::Error& e) {
        if (error) {
            *error = e.what();
        }
    }
}

size_t hash_value(const ClioResolverContext& context) {
    return std::hash<std::string>()(context.GetSettings());
}

PXR_NAMESPACE_CLOSE_SCOPE
