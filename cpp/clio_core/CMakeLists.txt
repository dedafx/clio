# clio_core: Perforce access and resolution rules. No USD, no Python.
# Linked statically into the Python extension and the USD plugin.

configure_file(
    include/clio/core/version.hpp.in
    "${CMAKE_CURRENT_BINARY_DIR}/generated/clio/core/version.hpp"
    @ONLY)

add_library(clio_core STATIC
    src/identifier.cpp
    src/pin.cpp
    src/resolver.cpp
    src/settings.cpp
    src/workspace.cpp
    src/p4/connection.cpp)
add_library(clio::core ALIAS clio_core)

target_include_directories(clio_core
    PUBLIC
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
        "${CMAKE_CURRENT_BINARY_DIR}/generated")

# P4API stays private: nothing outside clio_core includes its headers, so
# its macros and names never leak into USD or Python code. (CMake still
# passes the static libraries on to whatever links clio_core.)
target_link_libraries(clio_core PRIVATE P4API::P4API)

clio_set_warnings(clio_core)
