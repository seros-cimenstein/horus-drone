cmake_minimum_required(VERSION 3.10.2 FATAL_ERROR)

project(HorusCommander LANGUAGES CXX)

if(POLICY CMP0100)
  cmake_policy(SET CMP0100 NEW)
endif()

set(CMAKE_AUTOMOC ON)

find_package(Qt5
  COMPONENTS
    Core
    Quick
    QuickControls2
  REQUIRED
)
find_package(gz-gui8 REQUIRED)
find_package(gz-msgs10 REQUIRED)
find_package(gz-transport13 REQUIRED)

include(GNUInstallDirs)

set(GZ_GUI_VER ${gz-gui8_VERSION_MAJOR})
set(GZ_MSGS_VER ${gz-msgs10_VERSION_MAJOR})
set(GZ_TRANSPORT_VER ${gz-transport13_VERSION_MAJOR})
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} ${GZ-GUI_CXX_FLAGS}")

QT5_ADD_RESOURCES(resources_RCC HorusCommander.qrc)

add_library(HorusCommander SHARED
  HorusCommander.hh
  HorusCommander.cc
  ${resources_RCC}
)
set_property(TARGET HorusCommander PROPERTY CXX_STANDARD 17)
set_property(TARGET HorusCommander PROPERTY CXX_STANDARD_REQUIRED ON)

target_link_libraries(HorusCommander
  PRIVATE
    gz-gui${GZ_GUI_VER}::gz-gui${GZ_GUI_VER}
    gz-msgs${GZ_MSGS_VER}::gz-msgs${GZ_MSGS_VER}
    gz-transport${GZ_TRANSPORT_VER}::gz-transport${GZ_TRANSPORT_VER}
    Qt5::Core
    Qt5::Quick
    Qt5::QuickControls2
)

install(TARGETS HorusCommander
  LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
)
