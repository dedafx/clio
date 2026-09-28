#pragma once

#include <pxr/pxr.h>
#include <pxr/usd/ar/resolver.h>

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

/// ArResolver for the clio: URI scheme (design doc §10.3).
///
/// A thin adapter: identifiers, contexts and USD types live here; Perforce
/// work and resolution rules live in clio_core. One clio_core AssetResolver
/// (with its own connection) exists per distinct context.
///
/// Resolved paths are local file paths, so relative paths inside a resolved
/// layer are anchored on the filesystem by the primary resolver. Whether to
/// return clio:-form resolved paths instead is the open question in design
/// doc §10.4.
class ClioResolver final : public ArResolver {
public:
    ClioResolver();
    ~ClioResolver() override;

protected:
    std::string _CreateIdentifier(const std::string& assetPath,
                                  const ArResolvedPath& anchorAssetPath) const override;
    std::string _CreateIdentifierForNewAsset(const std::string& assetPath,
                                             const ArResolvedPath& anchorAssetPath) const override;
    ArResolvedPath _Resolve(const std::string& assetPath) const override;
    ArResolvedPath _ResolveForNewAsset(const std::string& assetPath) const override;

    ArResolverContext _CreateDefaultContext() const override;
    ArResolverContext _CreateDefaultContextForAsset(const std::string& assetPath) const override;
    ArResolverContext _CreateContextFromString(const std::string& contextStr) const override;
    bool _IsContextDependentPath(const std::string& assetPath) const override;
    void _RefreshContext(const ArResolverContext& context) override;

    ArAssetInfo _GetAssetInfo(const std::string& assetPath,
                              const ArResolvedPath& resolvedPath) const override;
    ArTimestamp _GetModificationTimestamp(const std::string& assetPath,
                                          const ArResolvedPath& resolvedPath) const override;
    std::shared_ptr<ArAsset> _OpenAsset(const ArResolvedPath& resolvedPath) const override;
    std::shared_ptr<ArWritableAsset> _OpenAssetForWrite(const ArResolvedPath& resolvedPath,
                                                        WriteMode writeMode) const override;

private:
    /// The core resolver for the context bound in this thread, or the
    /// default context. Null if no usable context is available.
    std::shared_ptr<clio::core::AssetResolver> _GetCoreResolver() const;
    std::shared_ptr<clio::core::AssetResolver> _GetCoreResolver(const ClioResolverContext& ctx) const;

    void _WarnOnce(const std::string& assetPath, const std::string& warning) const;

    mutable std::mutex _mutex;
    mutable std::map<std::string, std::shared_ptr<clio::core::AssetResolver>> _coreResolvers;
    mutable std::set<std::string> _warned; ///< paths already warned about, until refresh
};

PXR_NAMESPACE_CLOSE_SCOPE
