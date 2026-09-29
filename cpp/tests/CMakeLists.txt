include(FetchContent)

FetchContent_Declare(doctest
    GIT_REPOSITORY https://github.com/doctest/doctest.git
    GIT_TAG v2.4.11
    GIT_SHALLOW TRUE)
set(DOCTEST_NO_INSTALL ON CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(doctest)

# p4d for integration tests. Tests that need a server are skipped (with a
# message) when it is not found.
find_program(CLIO_P4D_EXECUTABLE p4d
    HINTS "${CLIO_P4API_ROOT}" "${CLIO_P4API_ROOT}/.." ENV CLIO_P4D
    DOC "p4d used to run a throwaway server in tests")

add_executable(clio_core_tests
    main.cpp
    p4d_fixture.cpp
    test_identifier.cpp
    test_pin.cpp
    test_settings.cpp
    test_workspace.cpp)

target_link_libraries(clio_core_tests PRIVATE clio::core doctest::doctest)
clio_set_warnings(clio_core_tests)

add_test(NAME clio_core_tests COMMAND clio_core_tests)
if(CLIO_P4D_EXECUTABLE)
    set_tests_properties(clio_core_tests PROPERTIES
        ENVIRONMENT "CLIO_TEST_P4D=${CLIO_P4D_EXECUTABLE}")
endif()
