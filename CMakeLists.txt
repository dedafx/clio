cmake_minimum_required(VERSION 3.24)

project(clio
    VERSION 0.1.0
    DESCRIPTION "Artist-friendly Perforce asset access with a USD asset resolver"
    LANGUAGES CXX)

# USD 25.08 and 26.08 are both built as C++17; plugins must match.
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Apply our visibility settings to fetched dependencies (doctest) too.
set(CMAKE_POLICY_DEFAULT_CMP0063 NEW)

list(APPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/cmake")

option(CLIO_BUILD_PYTHON "Build the deda.clio._core Python extension" ON)
option(CLIO_BUILD_USD "Build the clio: USD asset resolver plugin (needs pxr_DIR)" OFF)
option(CLIO_BUILD_TESTS "Build the C++ tests" ${PROJECT_IS_TOP_LEVEL})
option(CLIO_OPENSSL_STATIC
    "Link OpenSSL statically and keep its symbols private to Clio's binaries" ON)

set(CLIO_P4API_ROOT "$ENV{CLIO_P4API_ROOT}" CACHE PATH
    "Root of the extracted Perforce C++ API (contains include/p4 and lib)")

include(ClioCompilerOptions)

find_package(Threads REQUIRED)
find_package(P4API REQUIRED)

add_subdirectory(cpp/clio_core)

if(CLIO_BUILD_PYTHON)
    add_subdirectory(cpp/python)
endif()

if(CLIO_BUILD_USD)
    add_subdirectory(cpp/clio_usd)
endif()

if(CLIO_BUILD_TESTS)
    enable_testing()
    add_subdirectory(cpp/tests)
endif()
