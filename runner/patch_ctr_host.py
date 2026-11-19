"""Make 3dsrecomp's runtime a library Yata hosts (cmake -DYATA_HOST=ON with -DGAMES=...):

  libctrhost.dylib    the runtime; yata_ctr_main() boots the game Yata names (YATA_CTR_GAME: its folder,
                      YATA_CTR_CODE: its code, YATA_CTR_SAVE: where saves and the log go) and runs its loop on
                      the main thread, which keeps the run loop going so Yata's own screens go on working.
                      yata_ctr_suspend(1/0): under Yata's HOME Menu (paused, window hidden) / back.
                      SDL is Yata's own SDL3.framework (linked as @rpath/SDL3.framework/SDL3 afterwards).
  <game>.dylib        each game's code, linked against libctrhost.dylib.

The in-game menu gets "HOME Menu" (YATA_CTR_HOME, a function of Yata's) in place of "Choose another game";
Restart calls YATA_CTR_RESTART (Yata closes and starts the game again by itself).
usage: patch_ctr_host.py <3dsrecomp folder>"""
import os
import sys

R = sys.argv[1]


def patch(rel, edits):
    p = os.path.join(R, rel)
    s = open(p, encoding="utf-8").read()
    for old, new in edits:
        n = s.count(old)
        if n != 1:
            sys.exit(f"{rel}: expected one match, found {n}: {old[:70]!r}")
        s = s.replace(old, new)
    open(p, "w", encoding="utf-8").write(s)
    print("patched", rel)


patch("runtime/main.cpp", [
    ("#if TARGET_OS_IOS\n#include <SDL3/SDL_main.h>",
     "#if TARGET_OS_IOS && defined(YATA_HOST)\n#define SDL_MAIN_HANDLED  // Yata owns the app: no main() of SDL's\n"
     "#include <SDL3/SDL_main.h>\n#elif TARGET_OS_IOS\n#include <SDL3/SDL_main.h>"),
    ("// In an app bundle nobody sees stderr: show fatal errors in a dialog that says where the log is.",
     """#if defined(YATA_HOST) && defined(RECOMP_MODULES)
// Inside Yata: the game's code is the library Yata names (YATA_CTR_CODE).
static void yata_load_module(const char *lib, const std::string &id) {
    void *h = dlopen(lib, RTLD_NOW);
    if (!h) FATAL("cannot load %s: %s\\n", lib, dlerror());
    const auto entries = (const GuestEntry *)dlsym(h, "guest_entries");
    const auto count = (const u32 *)dlsym(h, "guest_entry_count");
    if (!entries || !count) FATAL("%s has no game code\\n", lib);
    set_game_code(entries, *count);
    const auto cros = (const GuestCro *)dlsym(h, "guest_cros");
    const auto cro_count = (const u32 *)dlsym(h, "guest_cro_count");
    if (cros && cro_count) set_game_cros(cros, *cro_count);
    const auto classes = (const GuestClass *)dlsym(h, "guest_classes");
    const auto class_count = (const u32 *)dlsym(h, "guest_class_count");
    if (classes && class_count) set_game_classes(classes, *class_count);
    g_start_game = id;
}
#endif

// In an app bundle nobody sees stderr: show fatal errors in a dialog that says where the log is."""),
    ("int main(int argc, char **argv) {\n    g_argv = argv;",
     "#ifdef YATA_HOST\nextern \"C\" __attribute__((visibility(\"default\"))) int yata_ctr_main(int argc, char **argv) {\n"
     "    SDL_SetMainReady();\n#else\nint main(int argc, char **argv) {\n#endif\n    g_argv = argv;"),
    ("    if (const std::string exe = platform::exe_path(); !exe.empty()) {\n        const auto exe_dir",
     """#if defined(YATA_HOST) && defined(RECOMP_MODULES)
    if (const char *gd = getenv("YATA_CTR_GAME")) {
        dir = gd;
        bundle = true;
        if (const char *sb = getenv("YATA_CTR_SAVE")) g_save_base = sb;
        const char *code = getenv("YATA_CTR_CODE");
        if (!code) FATAL("YATA_CTR_CODE is not set\\n");
        yata_load_module(code, std::filesystem::path(gd).filename().string());
    } else
#endif
    if (const std::string exe = platform::exe_path(); !exe.empty()) {
        const auto exe_dir"""),
])

patch("runtime/frontend.cpp", [
    ("static std::atomic<bool> in_background;  // (iOS) the app is not on screen: nothing runs or draws\n",
     """static std::atomic<bool> in_background;  // (iOS) the app is not on screen: nothing runs or draws

#ifdef YATA_HOST
// Inside Yata: under Yata's HOME Menu the game is paused and its window hidden, and this loop hands the main
// thread's run loop to Yata's screens (yata_ctr_suspend is called by Yata, on the main thread).
static std::atomic<bool> host_suspended;
extern "C" int CFRunLoopRunInMode(const void *mode, double seconds, unsigned char returnAfterSourceHandled);
extern "C" const void *const kCFRunLoopDefaultMode;
extern "C" __attribute__((visibility("default"))) void yata_ctr_suspend(int on) {
    host_suspended = on != 0;
    if (!window) return;
    if (on) {
        SDL_HideWindow(window);
    } else {
        SDL_ShowWindow(window);
        SDL_RaiseWindow(window);
    }
}
static void yata_host_call(const char *var) {  // a function of Yata's: its address in an environment variable
    if (const char *p = getenv(var))
        if (auto f = (void (*)())(uintptr_t)strtoull(p, nullptr, 16)) f();
}
extern "C" void yata_ctr_home() { yata_host_call("YATA_CTR_HOME"); }
#endif
"""),
    ("        paused = menu::is_open();\n        if (in_background) {",
     """        paused = menu::is_open();
#ifdef YATA_HOST
        if (host_suspended) {  // under Yata's HOME Menu: Yata's screens get the main thread
            paused = true;
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.008, 1);
            next_vblank = steady_clock::now();
            continue;
        }
#endif
        if (in_background) {"""),
    ("#if TARGET_OS_IOS\n    SDL_ShowSimpleMessageBox(SDL_MESSAGEBOX_INFORMATION, g_game.title.c_str(), ios_message, window);\n    _exit(0);\n#endif",
     """#ifdef YATA_HOST
    (void)ios_message;
    yata_host_call("YATA_CTR_RESTART");  // Yata closes and starts the game again by itself
    for (;;) CFRunLoopRunInMode(kCFRunLoopDefaultMode, 1.0, 0);
#elif TARGET_OS_IOS
    SDL_ShowSimpleMessageBox(SDL_MESSAGEBOX_INFORMATION, g_game.title.c_str(), ios_message, window);
    _exit(0);
#endif"""),
])

patch("runtime/menu.cpp", [
    ('        r.push_back({"Resume", "", "", nullptr, [] { toggle(); }});',
     '        r.push_back({"Resume", "", "", nullptr, [] { toggle(); }});\n'
     '#ifdef YATA_HOST\n        r.push_back({"HOME Menu", "", "", nullptr, [] { toggle(); yata_ctr_home(); }});\n#endif'),
    ("#ifdef RECOMP_MODULES\n        r.push_back({\"Choose another game\"",
     "#if defined(RECOMP_MODULES) && !defined(YATA_HOST)\n        r.push_back({\"Choose another game\""),
    ("namespace menu {", '#ifdef YATA_HOST\nextern "C" void yata_ctr_home();\n#endif\n\nnamespace menu {'),
])

patch("CMakeLists.txt", [
    ("if(GAMES)\n  set(APP recomp)\n  add_executable(${APP} ${RT_SRC})\n",
     """if(GAMES)
  set(APP recomp)
  if(YATA_HOST)
    # (Yata) the runtime as a library Yata loads, with the games' code beside it
    set(APP ctrhost)
    add_library(${APP} SHARED ${RT_SRC})
    set_target_properties(${APP} PROPERTIES INSTALL_NAME_DIR "@rpath" BUILD_WITH_INSTALL_NAME_DIR ON)
    target_compile_definitions(${APP} PRIVATE YATA_HOST)
    target_link_options(${APP} PRIVATE "-Wl,-rpath,@loader_path")
  else()
    add_executable(${APP} ${RT_SRC})
  endif()
"""),
    ('      target_link_options(${g} PRIVATE "SHELL:-bundle_loader $<TARGET_FILE:${APP}>")',
     """      if(YATA_HOST)
        target_link_libraries(${g} PRIVATE ${APP})
        target_link_options(${g} PRIVATE "-Wl,-rpath,@loader_path")
      else()
        target_link_options(${g} PRIVATE "SHELL:-bundle_loader $<TARGET_FILE:${APP}>")
      endif()"""),
    ("target_link_libraries(${APP} PRIVATE SDL3::SDL3-static",
     "if(YATA_HOST)\n    set(CTR_SDL SDL3::SDL3-shared)\n  else()\n    set(CTR_SDL SDL3::SDL3-static)\n  endif()\n"
     "  target_link_libraries(${APP} PRIVATE ${CTR_SDL}"),
])
print("ok")
