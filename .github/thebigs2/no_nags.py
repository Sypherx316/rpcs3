# Adapts an RPCS3 source tree (run from its root) to this fork's builds:
# - no "Experimental Build Warning" dialog (rpcs3/rpcs3qt/gui_application.cpp)
# - the updater (rpcs3/rpcs3qt/update_manager.cpp) follows this fork's Releases instead of the
#   official server, which would replace this build with an official one, without the fix. It reads
#   update.json from the newest Release (written by thebigs2-auto.yml in the official server's
#   format) and installs that Release's win64.7z / linux64.AppImage the official way. Setting
#   RPCS3_OFFICIAL_UPDATES=1 asks the official server again.
# A file or check that is not there is only a warning: the PS5 frontend uses neither.
import os
import re

UPDATE_JSON = "https://github.com/Sypherx316/rpcs3/releases/latest/download/update.json"


def patch(path, marker, pattern, replacement, what):
    if not os.path.exists(path):
        print(f"::warning::{path} not found, {what} not changed")
        return
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    if marker in text:
        print(f"{what}: already done")
        return
    nl = "\r\n" if "\r\n" in text else "\n"
    text, count = re.subn(pattern, lambda m: replacement(m).replace("\n", nl), text, count=1, flags=re.M)
    if count != 1:
        print(f"::warning::{what}: code not found, left as it is")
        return
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    print(f"{what}: done")


patch("rpcs3/rpcs3qt/gui_application.cpp", "No experimental build dialog",
      r"if \(!rpcs3::is_release_build\(\) && !rpcs3::is_local_build\(\)\)",
      lambda m: "if (false) // No experimental build dialog (thebigs2-fix)",
      "experimental build dialog")

updater = "rpcs3/rpcs3qt/update_manager.cpp"

patch(updater, "<QFileInfo>",
      r"^#include <QDateTime>\r?$",
      lambda m: m.group(0) + "\n#include <QFileInfo>",
      "updater: QFileInfo include")

patch(updater, "official_url",
      r'const std::string url = (fmt::format\("https://update\.rpcs3\.net/[^;]*;)',
      lambda m: "const std::string official_url = " + m.group(1) + "\n\n"
                "\t// Updates come from the update.json of github.com/Sypherx316/rpcs3's newest Release (thebigs2-fix);\n"
                "\t// RPCS3_OFFICIAL_UPDATES=1 asks the official server instead\n"
                "\tconst std::string url = qEnvironmentVariableIsSet(\"RPCS3_OFFICIAL_UPDATES\") ? official_url : \"" + UPDATE_JSON + "\";",
      "updater: source")

patch(updater, "thebigs2_update_answer(",
      r"^bool update_manager::handle_json\(",
      lambda m: "// thebigs2-fix: the update.json of github.com/Sypherx316/rpcs3's Releases carries the official server's\n"
                "// latest_build section (plus its build number); the rest of the answer for this build is worked out here\n"
                "static QJsonObject thebigs2_update_answer(QJsonObject json)\n"
                "{\n"
                "\tif (!json.contains(\"thebigs2\"))\n"
                "\t\treturn json;\n"
                "\n"
                "\tconst QJsonObject latest = json[\"latest_build\"].toObject();\n"
                "\tconst int latest_build = latest[\"build\"].toInt();\n"
                "\tconst int current_build = QString::fromStdString(rpcs3::get_commit_and_hash().first).toInt();\n"
                "\n"
                "\t// This build's date is when it was installed (UTC, read like the server's dates), kept before the\n"
                "\t// newer build's so the dialog reads \"behind\"\n"
                "\tconst QString date_fmt = QStringLiteral(\"yyyy-MM-dd hh:mm:ss\");\n"
                "\tconst QDateTime latest_date = QDateTime::fromString(latest[\"datetime\"].toString(), date_fmt);\n"
                "\tconst QString installed = QFileInfo(QString::fromStdString(fs::get_executable_path())).lastModified().toUTC().toString(date_fmt);\n"
                "\tQDateTime current_date = QDateTime::fromString(installed, date_fmt);\n"
                "\tif (latest_build > current_build && latest_date.isValid() && current_date >= latest_date)\n"
                "\t\tcurrent_date = latest_date.addSecs(-60);\n"
                "\n"
                "\tQJsonObject current;\n"
                "\tcurrent[\"version\"] = QStringLiteral(\"%1-%2\").arg(latest[\"version\"].toString().section('-', 0, 0)).arg(current_build);\n"
                "\tcurrent[\"datetime\"] = current_date.toString(date_fmt);\n"
                "\n"
                "\tjson[\"current_build\"] = current;\n"
                "\tjson[\"return_code\"] = latest_build > current_build ? 1 : 0;\n"
                "\treturn json;\n"
                "}\n"
                "\n" + m.group(0),
      "updater: update.json answer")

patch(updater, "thebigs2_update_answer(json_document",
      r"const QJsonObject json_data = json_document\.object\(\);",
      lambda m: "const QJsonObject json_data = thebigs2_update_answer(json_document.object());",
      "updater: use update.json answer")

patch(updater, "github.com/Sypherx316/rpcs3/releases/download/",
      r'if \(!m_request_url\.starts_with\("https://github\.com/RPCS3/rpcs3"\)\)',
      lambda m: "if (!m_request_url.starts_with(\"https://github.com/RPCS3/rpcs3\") &&\n"
                "\t\t!m_request_url.starts_with(\"https://github.com/Sypherx316/rpcs3/releases/download/\")) // thebigs2-fix",
      "updater: allow this fork's downloads")
