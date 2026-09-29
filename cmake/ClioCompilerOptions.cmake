# Shared compiler and linker settings for Clio targets.

# Everything Clio builds ends up inside a shared object (the Python extension
# or the USD plugin), so all code is position independent and hidden by default.
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)
set(CMAKE_VISIBILITY_INLINES_HIDDEN ON)

function(clio_set_warnings target)
    if(MSVC)
        target_compile_options(${target} PRIVATE /W4 /permissive-)
        # std::getenv is fine here: Clio never keeps the returned pointer.
        target_compile_definitions(${target} PRIVATE _CRT_SECURE_NO_WARNINGS)
    else()
        target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic)
    endif()
endfunction()

# Keep symbols from static dependencies (P4API, OpenSSL) out of a shared
# object's dynamic symbol table, so they cannot clash with other copies loaded
# in the same process (for example OpenSSL loaded by Python or a host app).
function(clio_hide_static_symbols target)
    if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
        target_link_options(${target} PRIVATE "LINKER:--exclude-libs,ALL")
    endif()
    # Windows: DLL symbols are private unless exported.
    # macOS: two-level namespaces keep static symbols private to the image.
endfunction()
