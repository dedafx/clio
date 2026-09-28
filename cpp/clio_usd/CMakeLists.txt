# clioUsd: the clio: ArResolver plugin. Built once per USD build.
#
# Layout (the same in the build tree, an install, and the wheel):
#   <dir>/clioUsd.so
#   <dir>/clioUsd/resources/plugInfo.json    <- add this folder to PXR_PLUGINPATH_NAME

find_package(pxr CONFIG REQUIRED)

set(CLIO_USD_MIN_VERSION 2508) # USD 25.08
math(EXPR _clio_pxr_version "${PXR_MINOR_VERSION} * 100 + ${PXR_PATCH_VERSION}")
if(_clio_pxr_version LESS CLIO_USD_MIN_VERSION)
    message(FATAL_ERROR "clioUsd needs USD 25.08 or later, found ${PXR_VERSION}")
endif()
string(REGEX REPLACE "^([0-9])$" "0\\1" _clio_pxr_patch "${PXR_PATCH_VERSION}")
message(STATUS "Building clioUsd against USD ${PXR_MINOR_VERSION}.${_clio_pxr_patch}")

add_library(clioUsd MODULE
    resolver.cpp
    resolverContext.cpp)

set_target_properties(clioUsd PROPERTIES PREFIX "")

# USD's registry macros need to know which library they are in.
target_compile_definitions(clioUsd PRIVATE
    MFB_PACKAGE_NAME=clioUsd
    MFB_ALT_PACKAGE_NAME=clioUsd
    MFB_PACKAGE_MODULE=ClioUsd)

target_link_libraries(clioUsd PRIVATE clio::core ar tf vt)
clio_hide_static_symbols(clioUsd)
clio_set_warnings(clioUsd)

set(CLIO_USD_LIBRARY_PATH "../clioUsd${CMAKE_SHARED_MODULE_SUFFIX}")
set(_clio_usd_build_dir "${CMAKE_BINARY_DIR}/usd_plugin")
configure_file(plugInfo.json.in "${_clio_usd_build_dir}/clioUsd/resources/plugInfo.json" @ONLY)
set_target_properties(clioUsd PROPERTIES LIBRARY_OUTPUT_DIRECTORY "${_clio_usd_build_dir}")

if(SKBUILD)
    set(_clio_usd_install_dir deda/clio/usd/plugin)
else()
    set(_clio_usd_install_dir plugin/usd)
endif()
install(TARGETS clioUsd LIBRARY DESTINATION "${_clio_usd_install_dir}")
install(FILES "${_clio_usd_build_dir}/clioUsd/resources/plugInfo.json"
    DESTINATION "${_clio_usd_install_dir}/clioUsd/resources")

