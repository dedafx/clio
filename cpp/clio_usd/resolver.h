#pragma once

#include <pxr/pxr.h>
#include <pxr/usd/ar/defaultResolver.h>

#include <filesystem>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>

namespace clio::core {
class AssetResolver;
}

PXR_NAMESPACE_OPEN_SCOPE

class ClioResolverContext;

/// USD's default resolver, enhanced by Clio (design doc §10.3).
///
/// Layers contain ordinary paths, so they open in any USD without Clio.
/// When this plugin is installed it becomes the primary resolver. With no
/// Clio context bound it behaves exactly like ArDefaultResolver. With a Clio
/// context bound, paths inside the project (the workspace root, or a pinned
/// version in the version store) are fetched from Perforce at the context's
/// pin before USD reads them; everything else, and anything Clio cannot
/// provide, is resolved by ArDefaultResolver.
///
/// A thin adapter: Perforce work and resolution rules live in clio_core. One
/// clio_core AssetResolver (with its own connection) exists per distinct
/// context.
class ClioResolver final : public ArDefaultResolver {
public:
    ClioResolver();
    ~ClioResolver() override;

protected:
    ArResolvedPath _Resolve(const std::string& assetPath) const override;

    ArResolverContext _CreateDefaultContext() const override;
    ArResolverContext _CreateDefaultContextForAsset(const std::string& assetPath) const override;
    ArResolverContext _CreateContextFromString(const std::string& contextStr) const override;
    void _RefreshContext(const ArResolverContext& context) override;
    bool _IsContextDependentPath(const std::string& assetPath) const override;

    ArAssetInfo _GetAssetInfo(const std::string& assetPath,
                              const ArResolvedPath& resolvedPath) const override;

private:
    /// The core resolver for the Clio context bound in this thread, or the
    /// default ($CLIO_RESOLVER_CONTEXT). Null if there is none.
    std::shared_ptr<clio::core::AssetResolver> _GetCoreResolver() const;
    std::shared_ptr<clio::core::AssetResolver> _GetCoreResolver(const ClioResolverContext& ctx) const;

    /// The project path Clio should handle for `assetPath`, if any.
    std::filesystem::path _ProjectPath(const clio::core::AssetResolver& core,
                                       const std::string& assetPath) const;

    void _WarnOnce(const std::string& assetPath, const std::string& warning) const;

    mutable std::mutex _mutex;
    mutable std::map<std::string, std::shared_ptr<clio::core::AssetResolver>> _coreResolvers;
    mutable std::set<std::string> _warned; ///< paths already warned about, until refresh
};

PXR_NAMESPACE_CLOSE_SCOPE
