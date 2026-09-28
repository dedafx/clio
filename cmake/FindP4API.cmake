#[=======================================================================[.rst:
FindP4API
---------

Find the Perforce C/C++ API (P4API), as distributed by Perforce in
``p4api-<platform>.tgz``.

Set ``CLIO_P4API_ROOT`` (or ``P4API_ROOT``) to the extracted directory, which
contains ``include/p4`` and ``lib``.

Imported target
^^^^^^^^^^^^^^^

``P4API::P4API``
  The client libraries plus OpenSSL and platform libraries, in link order.

Result variables
^^^^^^^^^^^^^^^^

``P4API_FOUND``, ``P4API_INCLUDE_DIR``, ``P4API_VERSION``
#]=======================================================================]

set(_p4api_hints ${CLIO_P4API_ROOT} ${P4API_ROOT} $ENV{P4API_ROOT})

find_path(P4API_INCLUDE_DIR
    NAMES clientapi.h
    HINTS ${_p4api_hints}
    PATH_SUFFIXES include/p4)

foreach(_lib client rpc supp p4script_cstub)
    find_library(P4API_${_lib}_LIBRARY
        NAMES ${_lib} lib${_lib}
        HINTS ${_p4api_hints}
        PATH_SUFFIXES lib)
    list(APPEND _p4api_required_libs P4API_${_lib}_LIBRARY)
endforeach()

# Version from the sample/Version file shipped with the API.
if(P4API_INCLUDE_DIR)
    get_filename_component(_p4api_root "${P4API_INCLUDE_DIR}/../.." ABSOLUTE)
    if(EXISTS "${_p4api_root}/sample/Version")
        file(STRINGS "${_p4api_root}/sample/Version" _p4api_release REGEX "^RELEASE")
        file(STRINGS "${_p4api_root}/sample/Version" _p4api_patch REGEX "^PATCHLEVEL")
        string(REGEX MATCHALL "[0-9]+" _p4api_release "${_p4api_release}")
        string(REGEX MATCH "[0-9]+" _p4api_patch "${_p4api_patch}")
        list(JOIN _p4api_release "." P4API_VERSION)
        set(P4API_VERSION "${P4API_VERSION}.${_p4api_patch}")
    endif()
endif()

if(CLIO_OPENSSL_STATIC)
    set(OPENSSL_USE_STATIC_LIBS TRUE)
endif()
find_package(OpenSSL 3 QUIET COMPONENTS SSL Crypto)

include(FindPackageHandleStandardArgs)
find_package_handle_standard_args(P4API
    REQUIRED_VARS P4API_INCLUDE_DIR ${_p4api_required_libs} OPENSSL_FOUND
    VERSION_VAR P4API_VERSION)

if(P4API_FOUND AND NOT TARGET P4API::P4API)
    add_library(P4API::P4API INTERFACE IMPORTED)
    target_include_directories(P4API::P4API SYSTEM INTERFACE "${P4API_INCLUDE_DIR}")
    target_link_libraries(P4API::P4API INTERFACE
        ${P4API_client_LIBRARY}
        ${P4API_rpc_LIBRARY}
        ${P4API_supp_LIBRARY}
        ${P4API_p4script_cstub_LIBRARY}
        OpenSSL::SSL
        OpenSSL::Crypto
        Threads::Threads
        ${CMAKE_DL_LIBS})
    if(WIN32)
        target_link_libraries(P4API::P4API INTERFACE ws2_32 crypt32 advapi32 user32 ole32 shell32)
    elseif(APPLE)
        target_link_libraries(P4API::P4API INTERFACE
            "-framework CoreFoundation" "-framework Foundation" "-framework Security")
    endif()
endif()

mark_as_advanced(P4API_INCLUDE_DIR
    P4API_client_LIBRARY P4API_rpc_LIBRARY P4API_supp_LIBRARY P4API_p4script_cstub_LIBRARY)
