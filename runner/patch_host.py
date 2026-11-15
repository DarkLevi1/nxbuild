"""Lets the iOS app's game run inside another app (a host that loads the executable as a library).

Hosted, the host sets three environment variables before calling the executable's main():
  NXH_RES     the folder holding the game's resources (the .app's contents: Info.plist, dm/, msl/, ...)
  NXH_DOC     the game's own documents folder (saves, log, romfs.img)
  NXH_READY   hex address of a function void ready(void *rootViewController, void (*focus)(int))
main() then sets the game up exactly as at an app launch, hands the host the game's root view
controller (retained) and its focus-event function, and returns instead of running UIApplicationMain.
Without NXH_READY nothing changes: the app runs as before.

usage: patch_host.py NXNATIVE_DIR"""
import sys
from pathlib import Path

root = Path(sys.argv[1])


def patch(rel, pairs):
    p = root / rel
    text = p.read_text(encoding="utf-8")
    for old, new, count in pairs:
        n = text.count(old)
        if n == 0 or (count and n != count):
            sys.exit(f"patch_host: {rel}: expected {count or 'some'} of {old[:60]!r}, found {n}")
        text = text.replace(old, new)
    p.write_text(text, encoding="utf-8")
    print(f"patched {rel}")


HELPERS = r'''
// Hosted inside another app (patch_host.py): the game's resources and documents are where the host says
static NSString *nxh_res(void) { const char *r = getenv("NXH_RES"); return r ? @(r) : NSBundle.mainBundle.resourcePath; }
static NSString *nxh_doc(void) { const char *d = getenv("NXH_DOC"); return d ? @(d) : NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES)[0]; }
static id nxh_info(NSString *k) {
    const char *r = getenv("NXH_RES");
    if (r) return [NSDictionary dictionaryWithContentsOfFile:[@(r) stringByAppendingPathComponent:@"Info.plist"]][k];
    return [NSBundle.mainBundle objectForInfoDictionaryKey:k];
}
'''

patch("dm/ios/Sources/main.m", [
    ('NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES)[0]', 'nxh_doc()', 0),
    ('NSBundle.mainBundle.resourcePath', 'nxh_res()', 0),
    ('[NSBundle.mainBundle objectForInfoDictionaryKey:@"DMLanguage"]', 'nxh_info(@"DMLanguage")', 1),
    ('int main(int argc, char **argv) {\n',
     'int main(int argc, char **argv) {\n'
     '    const char *nxh_ready = getenv("NXH_READY");\n'
     '    if (nxh_ready) {   // hosted (patch_host.py): set the game up, give the host its view, no UIApplicationMain\n'
     '        void (*ready)(void *, void (*)(int)) = (void (*)(void *, void (*)(int)))(uintptr_t)strtoull(nxh_ready, NULL, 16);\n'
     '        @autoreleasepool { g_boot_ok = boot_env(); }\n'
     '        @autoreleasepool { ready((__bridge_retained void *)front_ios_root(), front_ios_focus_event); }\n'
     '        return 0;\n'
     '    }\n', 1),
    # last, so the replacements above don't reach into the helpers' own fallbacks; it sits right after
    # `extern char **environ;`, before every function that uses it
    ('extern char **environ;\n', 'extern char **environ;\n' + HELPERS, 1),
])

patch("dm/ios/Sources/front_ios.m", [
    ('[[NSBundle.mainBundle objectForInfoDictionaryKey:@"DMShowFPS"] boolValue]',
     '[(getenv("NXH_RES") ? [NSDictionary dictionaryWithContentsOfFile:[@(getenv("NXH_RES")) stringByAppendingPathComponent:@"Info.plist"]][@"DMShowFPS"] '
     ': [NSBundle.mainBundle objectForInfoDictionaryKey:@"DMShowFPS"]) boolValue]', 1),
])

patch("dm/ios/Sources/GameMenu.swift", [
    # hosted: the in-game menu's first row goes to the host's HOME Menu (NXH_HOME: hex address of a
    # void (*)(void)); the touch gamepad has no HOME button of its own
    ('    if !placed { items.insert(UIMenu(options: .displayInline, children: [layout]), at: 0) }\n    return items\n}',
     '    if !placed { items.insert(UIMenu(options: .displayInline, children: [layout]), at: 0) }\n'
     '    if let h = getenv("NXH_HOME"), let addr = UInt(String(cString: h), radix: 16), addr != 0 {\n'
     '        let home = unsafeBitCast(addr, to: (@convention(c) () -> Void).self)\n'
     '        items.insert(UIMenu(options: .displayInline, children: [UIAction(title: "HOME Menu", image: UIImage(systemName: "house.fill")) { _ in home() }]), at: 0)\n'
     '    }\n'
     '    return items\n}', 1),
])

patch("dm/ios/Sources/Launcher.swift", [
    ('private func info(_ key: String) -> Any? { Bundle.main.object(forInfoDictionaryKey: key) }',
     'private func info(_ key: String) -> Any? {\n'
     '    if let r = getenv("NXH_RES") {   // hosted inside another app (patch_host.py)\n'
     '        return (NSDictionary(contentsOfFile: String(cString: r) + "/Info.plist") as? [String: Any])?[key]\n'
     '    }\n'
     '    return Bundle.main.object(forInfoDictionaryKey: key)\n'
     '}', 1),
    ('?? Bundle.main.resourceURL?.appendingPathComponent("dm")',
     '?? (getenv("NXH_RES").map { URL(fileURLWithPath: String(cString: $0)) } ?? Bundle.main.resourceURL)?.appendingPathComponent("dm")', 1),
])
