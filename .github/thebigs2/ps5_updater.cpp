// Self-updater for the PS5 title built by Sypherx316/rpcs3 (thebigs2-auto.yml).
//
// A few seconds after the title starts, it reads the newest Release's update.json. When its "ps5"
// build is newer than this one, it downloads the PS5 file, checks it with HMAC-SHA256 and decrypts
// it (AES-256-CTR) with the key in /data/rpcs3/tb2-update.key, unpacks it and replaces the title's
// files where ShadowMount installed it (/user/app/PPSA99300/mount.lnk). The new build runs the
// next time the title is opened. /data/rpcs3 (games, saves, settings) is never touched.
//
// The PS5 file is encrypted because this build may not be shared (RPCS3 GPL-2.0-only with
// PS5_Vulkan/PayloadSDK GPL-3.0): only the console that holds the key can use it.

#include "stdafx.h"

#include "Crypto/aes.h"
#include "Crypto/sha256.h"
#include "Crypto/unzip.h"
#include "Loader/TAR.h"
#include "Emu/RSX/Overlays/overlay_message.h"
#include "util/yaml.hpp"

#include <curl/curl.h>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstring>
#include <thread>

LOG_CHANNEL(upd_log, "PS5Update");

namespace
{
	// Filled in by .github/thebigs2/ps5_extras.py when the title is built
	constexpr u64 this_build = @TB2_BUILD@;
	constexpr std::string_view this_version = "@TB2_VERSION@";

	constexpr std::string_view update_url = "https://github.com/Sypherx316/rpcs3/releases/latest/download/update.json";
	constexpr std::string_view key_path = "/data/rpcs3/tb2-update.key";
	constexpr std::string_view work_dir = "/data/rpcs3/tb2-update/";
	constexpr std::string_view mount_link = "/user/app/PPSA99300/mount.lnk";
	constexpr std::string_view default_title_dir = "/data/homebrew/PPSA99300";
	constexpr char file_magic[8] = {'T', 'B', '2', 'P', 'S', '5', 1, 0};

	void notify(const std::string& text, u64 seconds = 8)
	{
		upd_log.notice("%s", text);
		rsx::overlays::queue_message(text, seconds * 1'000'000);
	}

	usz write_to_vector(char* data, usz size, usz nmemb, void* user)
	{
		auto& out = *static_cast<std::vector<u8>*>(user);
		out.insert(out.end(), data, data + size * nmemb);
		return size * nmemb;
	}

	// The title's libc getaddrinfo does not resolve names (see ps5_upstream_fixups.py, RPCN), so the
	// updater asks DNS servers itself over UDP for an A record
	std::string resolve_ipv4(const std::string& host)
	{
		std::vector<u8> query{0x7b, 0x2a, 0x01, 0x00, 0, 1, 0, 0, 0, 0, 0, 0};
		usz start = 0;
		while (start <= host.size())
		{
			usz end = host.find('.', start);
			if (end == umax) end = host.size();
			if (end - start == 0 || end - start > 63) return {};
			query.push_back(static_cast<u8>(end - start));
			query.insert(query.end(), host.begin() + start, host.begin() + end);
			start = end + 1;
		}
		query.insert(query.end(), {0, 0, 1, 0, 1});

		// Public resolvers, then the usual home router address
		for (const u32 server : {0x01010101u, 0x08080808u, 0xc0a80101u, 0x09090909u})
		{
			const int sock = ::socket(AF_INET, SOCK_DGRAM, 0);
			if (sock < 0)
				continue;

			timeval timeout{3, 0};
			::setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));

			sockaddr_in addr{};
			addr.sin_family = AF_INET;
			addr.sin_port = htons(53);
			addr.sin_addr.s_addr = htonl(server);

			u8 reply[1500];
			ssize_t got = -1;
			if (::sendto(sock, query.data(), query.size(), 0, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) == static_cast<ssize_t>(query.size()))
				got = ::recvfrom(sock, reply, sizeof(reply), 0, nullptr, nullptr);
			::close(sock);

			if (got < 12 || reply[0] != 0x7b || reply[1] != 0x2a || (reply[3] & 0x0f) != 0)
				continue;

			const auto skip_name = [&](usz pos) -> usz
			{
				while (pos < static_cast<usz>(got))
				{
					if ((reply[pos] & 0xc0) == 0xc0) return pos + 2;
					if (reply[pos] == 0) return pos + 1;
					pos += reply[pos] + 1;
				}
				return umax;
			};

			usz pos = skip_name(12);
			if (pos == umax) continue;
			pos += 4;

			const u32 answers = (reply[6] << 8) | reply[7];
			for (u32 i = 0; i < answers && pos != umax && pos + 10 <= static_cast<usz>(got); i++)
			{
				pos = skip_name(pos);
				if (pos == umax || pos + 10 > static_cast<usz>(got)) break;
				const u32 type = (reply[pos] << 8) | reply[pos + 1];
				const u32 len = (reply[pos + 8] << 8) | reply[pos + 9];
				pos += 10;
				if (type == 1 && len == 4 && pos + 4 <= static_cast<usz>(got))
					return fmt::format("%u.%u.%u.%u", reply[pos], reply[pos + 1], reply[pos + 2], reply[pos + 3]);
				pos += len;
			}
		}

		upd_log.warning("Could not resolve %s", host);
		return {};
	}

	std::string url_host(const std::string& url)
	{
		const usz begin = url.find("://");
		if (begin == umax) return {};
		const usz end = url.find_first_of(":/?", begin + 3);
		return url.substr(begin + 3, end == umax ? umax : end - begin - 3);
	}

	bool download_once(const std::string& url, std::vector<u8>& out, const std::string& ca_file, usz max_size, std::string& redirect);

	// GET into memory, following redirects (GitHub sends Release downloads to another host)
	bool download(std::string url, std::vector<u8>& out, const std::string& ca_file, usz max_size)
	{
		for (int hop = 0; hop < 8; hop++)
		{
			std::string redirect;
			if (!download_once(url, out, ca_file, max_size, redirect))
				return false;
			if (redirect.empty())
				return true;
			url = std::move(redirect);
		}
		return false;
	}

	// One request. TLS is checked against the CA list shipped beside the eboot; without it the
	// download still cannot be tampered with, because the PS5 file is authenticated with HMAC
	bool download_once(const std::string& url, std::vector<u8>& out, const std::string& ca_file, usz max_size, std::string& redirect)
	{
		const std::string host = url_host(url);
		const std::string ip = resolve_ipv4(host);
		if (ip.empty())
		{
			return false;
		}

		for (int verify = 1; verify >= 0; verify--)
		{
			out.clear();

			CURL* curl = curl_easy_init();
			if (!curl)
			{
				return false;
			}

			curl_slist* resolve = curl_slist_append(nullptr, fmt::format("%s:443:%s", host, ip).c_str());

			char err_buf[CURL_ERROR_SIZE]{};
			curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
			curl_easy_setopt(curl, CURLOPT_RESOLVE, resolve);
			curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 0L);
			curl_easy_setopt(curl, CURLOPT_USERAGENT, "rpcs3-ps5-updater");
			curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 20L);
			curl_easy_setopt(curl, CURLOPT_LOW_SPEED_LIMIT, 1024L);
			curl_easy_setopt(curl, CURLOPT_LOW_SPEED_TIME, 60L);
			curl_easy_setopt(curl, CURLOPT_MAXFILESIZE_LARGE, static_cast<curl_off_t>(max_size));
			curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_to_vector);
			curl_easy_setopt(curl, CURLOPT_WRITEDATA, &out);
			curl_easy_setopt(curl, CURLOPT_ERRORBUFFER, err_buf);

			if (verify && fs::is_file(ca_file))
			{
				curl_easy_setopt(curl, CURLOPT_CAINFO, ca_file.c_str());
			}
			else
			{
				curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 0L);
				curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 0L);
			}

			const CURLcode res = curl_easy_perform(curl);
			long status = 0;
			curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
			char* location = nullptr;
			if (res == CURLE_OK && status >= 300 && status < 400)
				curl_easy_getinfo(curl, CURLINFO_REDIRECT_URL, &location);
			redirect = location ? location : "";
			curl_easy_cleanup(curl);
			curl_slist_free_all(resolve);

			if (res == CURLE_OK && (status == 200 || !redirect.empty()))
			{
				return true;
			}

			upd_log.warning("Download of %s failed (verify=%d): curl %d (%s) %s, HTTP %d", url, verify, static_cast<int>(res), curl_easy_strerror(res), err_buf, static_cast<int>(status));

			const bool tls_error = res == CURLE_PEER_FAILED_VERIFICATION || res == CURLE_SSL_CACERT_BADFILE ||
				res == CURLE_SSL_CONNECT_ERROR || res == CURLE_SSL_CERTPROBLEM;

			if (!tls_error)
			{
				return false;
			}
		}

		return false;
	}

	std::vector<u8> sha256(const u8* a, usz a_size, const u8* b, usz b_size)
	{
		std::vector<u8> out(32);
		mbedtls_sha256_context ctx;
		mbedtls_sha256_init(&ctx);
		mbedtls_sha256_starts_ret(&ctx, 0);
		mbedtls_sha256_update_ret(&ctx, a, a_size);
		mbedtls_sha256_update_ret(&ctx, b, b_size);
		mbedtls_sha256_finish_ret(&ctx, out.data());
		mbedtls_sha256_free(&ctx);
		return out;
	}

	std::vector<u8> hmac_sha256(const u8 key[32], const u8* data, usz size)
	{
		u8 ipad[64]{}, opad[64]{};
		std::memcpy(ipad, key, 32);
		std::memcpy(opad, key, 32);
		for (u8& c : ipad) c ^= 0x36;
		for (u8& c : opad) c ^= 0x5c;

		const std::vector<u8> inner = sha256(ipad, sizeof(ipad), data, size);
		return sha256(opad, sizeof(opad), inner.data(), inner.size());
	}

	bool read_key(u8 aes_key[32], u8 mac_key[32])
	{
		const std::string text = fs::file(std::string(key_path)).to_string();
		std::string hex;
		for (char c : text)
		{
			if (std::isxdigit(static_cast<unsigned char>(c)))
				hex += c;
		}

		if (hex.size() != 128)
		{
			return false;
		}

		for (usz i = 0; i < 64; i++)
		{
			const u8 byte = static_cast<u8>(std::stoul(hex.substr(i * 2, 2), nullptr, 16));
			(i < 32 ? aes_key[i] : mac_key[i - 32]) = byte;
		}

		return true;
	}

	std::string title_dir()
	{
		std::string dir = fs::file(std::string(mount_link)).to_string();
		while (!dir.empty() && (dir.back() == '\n' || dir.back() == '\r' || dir.back() == ' ' || dir.back() == '\0' || dir.back() == '/'))
			dir.pop_back();
		return fs::is_dir(dir) ? dir : std::string(default_title_dir);
	}

	// Copies every file of src into dst (new name, then rename over the old one), eboot.bin last
	bool install_tree(const std::string& src, const std::string& dst, usz& count)
	{
		std::vector<std::string> files;
		std::vector<std::string> pending{""};

		while (!pending.empty())
		{
			const std::string rel = pending.back();
			pending.pop_back();

			for (const auto& entry : fs::dir(src + rel))
			{
				if (entry.name == "." || entry.name == "..")
					continue;

				const std::string child = rel + "/" + entry.name;
				if (entry.is_directory)
					pending.push_back(child);
				else
					files.push_back(child);
			}
		}

		std::stable_partition(files.begin(), files.end(), [](const std::string& f) { return f != "/eboot.bin"; });

		for (const std::string& rel : files)
		{
			const std::string target = dst + rel;
			const std::string temp = target + ".tb2new";

			if (!fs::create_path(fs::get_parent_dir(target)) || !fs::copy_file(src + rel, temp, true) || !fs::rename(temp, target, true))
			{
				upd_log.error("Could not install %s (%s)", target, fs::g_tls_error);
				fs::remove_file(temp);
				return false;
			}

			count++;
		}

		return true;
	}

	void run()
	{
		// Let Big Picture Mode come up first
		std::this_thread::sleep_for(std::chrono::seconds(6));

		upd_log.notice("This build: RPCS3 %s (PS5 build %u). Checking %s", this_version, this_build, update_url);

		const std::string dir = title_dir();
		const std::string ca_file = dir + "/cacert.pem";

		std::vector<u8> json;
		if (!download(std::string(update_url), json, ca_file, 1 << 20))
		{
			upd_log.warning("Could not check for updates");
			return;
		}

		YAML::Node ps5;
		{
			auto [root, error] = yaml_load(std::string(json.begin(), json.end()));
			if (!error.empty())
			{
				upd_log.error("update.json could not be read: %s", error);
				return;
			}
			ps5 = root["latest_build"]["ps5"];
		}

		if (!ps5 || !ps5.IsMap())
		{
			upd_log.notice("The newest Release has no PS5 build");
			return;
		}

		const u64 build = ps5["build"].as<u64>(0);
		const std::string version = ps5["version"].as<std::string>("");
		const std::string url = ps5["download"].as<std::string>("");
		const usz size = ps5["size"].as<usz>(0);

		if (build <= this_build || url.empty())
		{
			upd_log.notice("Up to date (newest PS5 build: %s)", version);
			return;
		}

		u8 aes_key[32], mac_key[32];
		if (!read_key(aes_key, mac_key))
		{
			notify(fmt::format("RPCS3 %s disponible, pero falta la clave %s", version, key_path), 12);
			return;
		}

		notify(fmt::format("Descargando RPCS3 %s para PS5...", version));

		std::vector<u8> data;
		if (!download(url, data, ca_file, 1ull << 30) || (size && data.size() != size))
		{
			notify("La actualizacion de RPCS3 no se pudo descargar");
			return;
		}

		constexpr usz head = sizeof(file_magic) + 16;
		if (data.size() < head + 32 || std::memcmp(data.data(), file_magic, sizeof(file_magic)) != 0)
		{
			notify("La actualizacion de RPCS3 no es valida");
			return;
		}

		const usz body = data.size() - 32;
		const std::vector<u8> mac = hmac_sha256(mac_key, data.data(), body);

		u8 diff = 0;
		for (usz i = 0; i < 32; i++)
			diff |= mac[i] ^ data[body + i];

		if (diff)
		{
			notify("La actualizacion de RPCS3 no coincide con tu clave");
			return;
		}

		u8 counter[16], stream[16]{};
		std::memcpy(counter, data.data() + sizeof(file_magic), 16);
		usz offset = 0;
		aes_context aes;
		aes_setkey_enc(&aes, aes_key, 256);
		aes_crypt_ctr(&aes, body - head, &offset, counter, stream, data.data() + head, data.data() + head);

		std::vector<u8> tar = unzip(data.data() + head, body - head);
		data = {};

		if (tar.empty())
		{
			notify("La actualizacion de RPCS3 no se pudo descomprimir");
			return;
		}

		const std::string work(work_dir);
		fs::remove_all(work, false, true);
		fs::create_path(work);

		const std::string tar_path = work + "ps5.tar";
		if (!fs::write_file(tar_path, fs::rewrite, tar) || !extract_tar(tar_path, work + "stage/"))
		{
			notify("La actualizacion de RPCS3 no se pudo desempaquetar");
			return;
		}

		tar = {};
		fs::remove_file(tar_path);

		usz count = 0;
		if (!install_tree(work + "stage/PPSA99300", dir, count))
		{
			notify("La actualizacion de RPCS3 no se pudo instalar (revisa RPCS3.log)", 12);
			return;
		}

		fs::remove_all(work, true, true);
		upd_log.success("Installed RPCS3 %s (PS5 build %u): %u files in %s", version, build, count, dir);
		notify(fmt::format("RPCS3 actualizado a %s. Cierra y vuelve a abrir RPCS3 para usarlo.", version), 20);
	}
}

namespace ps5_updater
{
	void start()
	{
		// The PS5 title is built without exceptions: run() reports its own failures
		std::thread([]()
		{
			run();
		}).detach();
	}
}
