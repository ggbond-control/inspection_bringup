cmake_minimum_required(VERSION 3.8)
project(inspection_bringup)

find_package(ament_cmake REQUIRED)

install(DIRECTORY
  config
  launch
  scripts
  systemd
  DESTINATION share/${PROJECT_NAME}
)

if(BUILD_TESTING)
  find_package(ament_cmake_test REQUIRED)
  find_package(Python3 REQUIRED COMPONENTS Interpreter)
  add_test(NAME navigation_charge_handoff_test
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/test/navigation_charge_handoff_test.py)
  set_tests_properties(navigation_charge_handoff_test PROPERTIES TIMEOUT 60 RUN_SERIAL TRUE)
endif()

ament_package()
