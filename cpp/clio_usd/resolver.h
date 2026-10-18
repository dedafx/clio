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

    // Write side (design doc §10.6). Saving a layer is a local action: Clio
    // opens the file for edit (or for add, once it is written) and refuses a
    // save that someone else's lock forbids. It never submits.
    //
    // Sdf calls _CanWriteAssetToPath as a plain question (SdfLayer::
    // PermissionToSave, with no whyNot), so it must stay cheap and free of
    // side effects: it only refuses stored versions. The Perforce work
    // happens in _OpenAssetForWrite, before anything is written.
    ArResolvedPath _ResolveForNewAsset(const std::string& assetPath) const override;
    bool _CanWriteAssetToPath(const ArResolvedPath& resolvedPath, std::string* whyNot) const override;
    std::shared_ptr<ArWritableAsset> _OpenAssetForWrite(const ArResolvedPath& resolvedPath,
                                                        WriteMode writeMode) const override;

private:
    /// The core resolver for the Clio context bound in this thread, or the
    /// default ($CLIO_RESOLVER_CONTEXT). Null if there is none.
    std::shared_ptr<clio::core::AssetResolver> _GetCoreResolver() const;
    std::shared_ptr<clio::core::AssetResolver> _GetCoreResolver(const ClioResolverContext& ctx) const;

    /// The project path Clio should handle for `assetPath`, if any.
    std::filesystem::path _ProjectPath(const clio::core::AssetResolver& core,
                                       const std::string& assetPath) const;

    void _WarnOnce(const std::string& assetPath, const std::string& warning) const;

    /// Open `resolvedPath` for edit if it is a project file. False (with the
    /// reason) if it must not be written. `*isNew` is set if the file is not
    /// in Perforce yet and must be added once it is written.
    bool _PrepareForWrite(const ArResolvedPath& resolvedPath, bool* isNew, std::string* whyNot) const;

    mutable std::mutex _mutex;
    mutable std::map<std::string, std::shared_ptr<clio::core::AssetResolver>> _coreResolvers;
    mutable std::set<std::string> _warned; ///< paths already warned about, until refresh

};

PXR_NAMESPACE_CLOSE_SCOPE
