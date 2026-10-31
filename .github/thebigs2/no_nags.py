# Removes what gets in the way of this build, in an RPCS3 source tree (run from its root):
# - the "Experimental Build Warning" dialog (rpcs3/rpcs3qt/gui_application.cpp)
# - the official updater (rpcs3/rpcs3qt/update_manager.cpp): it would replace this build with an
#   official one, without the fix. Updates come from this fork's Releases instead; setting
#   RPCS3_OFFICIAL_UPDATES=1 brings the official updater back.
# A file or check that is not there is only a warning: the PS5 frontend uses neither.
import os
import re


def patch(path, pattern, replacement, what):
    if not os.path.exists(path):
        print(f"::warning::{path} not found, {what} not changed")
        return
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    if "thebigs2-fix" in text:
        print(f"{what}: already done")
        return
    text, count = re.subn(pattern, replacement, text, count=1, flags=re.M)
    if count != 1:
        print(f"::warning::{what}: code not found, left as it is")
        return
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    print(f"{what}: done")


patch("rpcs3/rpcs3qt/gui_application.cpp",
      r"if \(!rpcs3::is_release_build\(\) && !rpcs3::is_local_build\(\)\)",
      "if (false) // No experimental build dialog (thebigs2-fix)",
      "experimental build dialog")

patch("rpcs3/rpcs3qt/update_manager.cpp",
      r"^(void update_manager::check_for_updates\(bool automatic,[^\n]*\r?\n\{\r?\n)",
      r"\1"
      "\t// Updates come from github.com/Sypherx316/rpcs3/releases (thebigs2-fix), not the official server\n"
      "\tif (!qEnvironmentVariableIsSet(\"RPCS3_OFFICIAL_UPDATES\"))\n"
      "\t{\n"
      "\t\tif (!automatic)\n"
      "\t\t\tQMessageBox::information(parent, tr(\"Auto-updater\"), tr(\"This build (The Bigs 2 fix) updates itself from github.com/Sypherx316/rpcs3/releases.\"));\n"
      "\t\treturn;\n"
      "\t}\n\n",
      "official updater")
