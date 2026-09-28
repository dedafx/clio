# deda.clio._core: the Python extension over clio_core.

find_package(Python 3.13 REQUIRED COMPONENTS Interpreter Development.Module
    OPTIONAL_COMPONENTS Development.SABIModule)

if(NOT nanobind_DIR)
    execute_process(
        COMMAND "${Python_EXECUTABLE}" -m nanobind --cmake_dir
        OUTPUT_VARIABLE nanobind_DIR
        OUTPUT_STRIP_TRAILING_WHITESPACE
        RESULT_VARIABLE _nanobind_result)
    if(NOT _nanobind_result EQUAL 0)
        message(FATAL_ERROR "nanobind not found: pip install nanobind, or set nanobind_DIR")
    endif()
endif()
find_package(nanobind CONFIG REQUIRED)

# STABLE_ABI builds one abi3 module for Python 3.13 and later (nanobind
# supports the stable ABI from Python 3.12).
nanobind_add_module(clio_python_core
    STABLE_ABI
    NB_STATIC
    module.cpp)
set_target_properties(clio_python_core PROPERTIES OUTPUT_NAME _core)

target_link_libraries(clio_python_core PRIVATE clio::core)
clio_hide_static_symbols(clio_python_core)

# Under scikit-build-core the wheel root is the install prefix.
if(SKBUILD)
    install(TARGETS clio_python_core LIBRARY DESTINATION deda/clio)
else()
    install(TARGETS clio_python_core LIBRARY DESTINATION python/deda/clio)
endif()
