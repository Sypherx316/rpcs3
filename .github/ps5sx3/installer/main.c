/* PS5SX3 update fetcher: runs in place of PS5SX3-Installer.elf.
 *
 * PS5SX3 starts "PS5SX3-Installer.elf" from its folder to install a new build. This program takes that
 * place and does one extra thing first: the build this repository publishes is encrypted, so it
 * downloads it, checks it with the key kept on the console, decrypts it and leaves the plain zip where
 * the real installer looks for a finished download. Then it starts the real installer
 * (PS5SX3-Installer-stock.elf) exactly the way PS5SX3 would have. The real installer finds the zip,
 * sees that its SHA-256 is the one latest.json gives, and installs it with its usual backup.
 *
 * Nothing in PS5SX3's folder is written by this program. If anything here fails, the real installer is
 * still started; it then cannot find the zip, says so, and changes nothing.
 *
 * Package: "TB2PS5" 01 00 | 16-byte counter | AES-256-CTR(zip) | HMAC-SHA256 of everything before it.
 * Key: /data/rpcs3/tb2-update.key, 128 hex characters (AES key, then HMAC key).
 */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include <curl/curl.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>

#define BLOCK_SIZE 0x238
#define BLOCK_URL 0x38
#define MARKER "PS5SX3-INSTALLER-LAUNCH-1"
#define MARKER_LEN 25

#define WORK_DIR "/data/PS5SX3-Installer"
#define DOWNLOAD_DIR WORK_DIR "/download"
#define LOG_FILE WORK_DIR "/tb2-fetch.log"
#define KEY_FILE "/data/rpcs3/tb2-update.key"
#define MOUNT_LINK "/user/app/PPSA99303/mount.lnk"
#define DEFAULT_TITLE_DIR "/data/homebrew/PPSA99303"
#define STOCK_NAME "PS5SX3-Installer-stock.elf"
#define ELF_LOADER_PORT 9021
#define MAX_PACKAGE (400LL * 1024 * 1024)

/* PS5SX3 looks for this block in the file, fills it in and sends the file to the ELF loader. It must be
 * in the file exactly once, zeroed after the name. See README: "launch block". */
__attribute__((used)) static volatile unsigned char launch_block[BLOCK_SIZE] = MARKER;

extern const unsigned char ca_bundle[];
extern const unsigned int ca_bundle_len;

typedef struct notify_request {
  char useless1[45];
  char message[3075];
} notify_request_t;

int sceKernelSendNotificationRequest(int, notify_request_t *, size_t, int);

static void logf_(const char *fmt, ...) {
  char line[1024];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(line, sizeof line, fmt, ap);
  va_end(ap);

  char stamp[32];
  time_t now = time(NULL);
  struct tm tm;
  gmtime_r(&now, &tm);
  strftime(stamp, sizeof stamp, "%Y-%m-%dT%H:%M:%SZ", &tm);

  printf("[tb2-fetch] %s\n", line);
  FILE *f = fopen(LOG_FILE, "a");
  if (f) {
    fprintf(f, "%s [%d] %s\n", stamp, (int)getpid(), line);
    fclose(f);
  }
}

static void notify(const char *fmt, ...) {
  notify_request_t req;
  memset(&req, 0, sizeof req);
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(req.message, sizeof req.message, fmt, ap);
  va_end(ap);
  logf_("notification: %s", req.message);
  sceKernelSendNotificationRequest(0, &req, sizeof req, 0);
}

/* ---------- small helpers ---------- */

static int read_file(const char *path, unsigned char **out, size_t *size, size_t limit) {
  FILE *f = fopen(path, "rb");
  if (!f)
    return -1;
  fseek(f, 0, SEEK_END);
  long n = ftell(f);
  fseek(f, 0, SEEK_SET);
  if (n < 0 || (size_t)n > limit) {
    fclose(f);
    return -1;
  }
  unsigned char *buf = malloc((size_t)n + 1);
  if (!buf) {
    fclose(f);
    return -1;
  }
  if (fread(buf, 1, (size_t)n, f) != (size_t)n) {
    fclose(f);
    free(buf);
    return -1;
  }
  fclose(f);
  buf[n] = 0;
  *out = buf;
  *size = (size_t)n;
  return 0;
}

static int hex_to_bytes(const char *hex, unsigned char *out, size_t count) {
  for (size_t i = 0; i < count; i++) {
    unsigned v;
    if (sscanf(hex + i * 2, "%2x", &v) != 1)
      return -1;
    out[i] = (unsigned char)v;
  }
  return 0;
}

static void bytes_to_hex(const unsigned char *in, size_t count, char *out) {
  for (size_t i = 0; i < count; i++)
    sprintf(out + i * 2, "%02x", in[i]);
}

/* Value of "key" in a flat JSON object: a string (copied without its quotes) or a number (as text).
 * Good enough for latest.json, which this repository writes itself. */
static int json_get(const char *json, const char *key, char *out, size_t out_size) {
  char pat[64];
  snprintf(pat, sizeof pat, "\"%s\"", key);
  const char *p = strstr(json, pat);
  if (!p)
    return -1;
  p += strlen(pat);
  while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n')
    p++;
  if (*p++ != ':')
    return -1;
  while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n')
    p++;
  size_t n = 0;
  if (*p == '"') {
    for (p++; *p && *p != '"'; p++) {
      if (*p == '\\' || n + 1 >= out_size)
        return -1; /* no escapes in the values we need */
      out[n++] = *p;
    }
    if (*p != '"')
      return -1;
  } else {
    for (; *p && strchr("0123456789", *p); p++) {
      if (n + 1 >= out_size)
        return -1;
      out[n++] = *p;
    }
    if (n == 0)
      return -1;
  }
  out[n] = 0;
  return 0;
}

/* ---------- network ---------- */

/* A record for host from a public DNS server, for consoles whose own DNS setting blocks or fails. */
static int resolve_ipv4(const char *host, char *ip, size_t ip_size) {
  unsigned char query[300] = {0x7b, 0x2a, 0x01, 0x00, 0, 1, 0, 0, 0, 0, 0, 0};
  size_t qlen = 12;
  const char *label = host;
  while (*label) {
    const char *dot = strchr(label, '.');
    size_t len = dot ? (size_t)(dot - label) : strlen(label);
    if (len == 0 || len > 63 || qlen + len + 6 > sizeof query)
      return -1;
    query[qlen++] = (unsigned char)len;
    memcpy(query + qlen, label, len);
    qlen += len;
    label += len + (dot ? 1 : 0);
  }
  static const unsigned char tail[] = {0, 0, 1, 0, 1};
  memcpy(query + qlen, tail, sizeof tail);
  qlen += sizeof tail;

  static const uint32_t servers[] = {0x01010101u, 0x08080808u, 0x09090909u, 0xc0a80101u};
  for (size_t s = 0; s < sizeof servers / sizeof servers[0]; s++) {
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0)
      continue;
    struct timeval timeout = {3, 0};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout);
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons(53);
    addr.sin_addr.s_addr = htonl(servers[s]);

    unsigned char reply[1500];
    ssize_t got = -1;
    if (sendto(sock, query, qlen, 0, (struct sockaddr *)&addr, sizeof addr) == (ssize_t)qlen)
      got = recvfrom(sock, reply, sizeof reply, 0, NULL, NULL);
    close(sock);
    if (got < 12 || reply[0] != 0x7b || reply[1] != 0x2a || (reply[3] & 0x0f) != 0)
      continue;

    size_t pos = 12;
    /* question: name, type, class */
    while (pos < (size_t)got && reply[pos] != 0 && (reply[pos] & 0xc0) != 0xc0)
      pos += reply[pos] + 1;
    pos += ((pos < (size_t)got && (reply[pos] & 0xc0) == 0xc0) ? 2 : 1) + 4;
    unsigned answers = (reply[6] << 8) | reply[7];
    for (unsigned i = 0; i < answers && pos + 10 <= (size_t)got; i++) {
      while (pos < (size_t)got && reply[pos] != 0 && (reply[pos] & 0xc0) != 0xc0)
        pos += reply[pos] + 1;
      pos += (pos < (size_t)got && (reply[pos] & 0xc0) == 0xc0) ? 2 : 1;
      if (pos + 10 > (size_t)got)
        break;
      unsigned type = (reply[pos] << 8) | reply[pos + 1];
      unsigned len = (reply[pos + 8] << 8) | reply[pos + 9];
      pos += 10;
      if (type == 1 && len == 4 && pos + 4 <= (size_t)got) {
        snprintf(ip, ip_size, "%u.%u.%u.%u", reply[pos], reply[pos + 1], reply[pos + 2], reply[pos + 3]);
        return 0;
      }
      pos += len;
    }
  }
  return -1;
}

struct sink {
  FILE *file;           /* when set, the body goes here */
  char *mem;            /* otherwise here */
  size_t size, limit;
  int overflow;
};

static size_t on_body(char *data, size_t size, size_t nmemb, void *user) {
  struct sink *s = user;
  size_t n = size * nmemb;
  if (s->size + n > s->limit) {
    s->overflow = 1;
    return 0;
  }
  if (s->file) {
    if (fwrite(data, 1, n, s->file) != n)
      return 0;
  } else {
    char *grown = realloc(s->mem, s->size + n + 1);
    if (!grown)
      return 0;
    s->mem = grown;
    memcpy(s->mem + s->size, data, n);
    s->mem[s->size + n] = 0;
  }
  s->size += n;
  return n;
}

static void url_host(const char *url, char *host, size_t host_size) {
  const char *p = strstr(url, "://");
  p = p ? p + 3 : url;
  size_t n = strcspn(p, ":/?#");
  if (n >= host_size)
    n = host_size - 1;
  memcpy(host, p, n);
  host[n] = 0;
}

/* One request, redirects not followed. Returns the HTTP status, or -1. In 'next' the redirect target. */
static long http_once(const char *url, struct sink *sink, int verify, int own_dns, char *next, size_t next_size,
                      CURLcode *code) {
  CURL *curl = curl_easy_init();
  if (!curl)
    return -1;
  struct curl_slist *resolve = NULL;
  char errbuf[CURL_ERROR_SIZE] = "";
  if (own_dns) {
    char host[256], ip[32], entry[320];
    url_host(url, host, sizeof host);
    if (resolve_ipv4(host, ip, sizeof ip) == 0) {
      snprintf(entry, sizeof entry, "%s:443:%s", host, ip);
      resolve = curl_slist_append(NULL, entry);
      curl_easy_setopt(curl, CURLOPT_RESOLVE, resolve);
    } else {
      logf_("no public DNS server answered for %s", host);
    }
  }
  curl_easy_setopt(curl, CURLOPT_URL, url);
  curl_easy_setopt(curl, CURLOPT_PROTOCOLS_STR, "https");
  curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 0L);
  curl_easy_setopt(curl, CURLOPT_USERAGENT, "ps5sx3-tb2-fetch/1");
  curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 20L);
  curl_easy_setopt(curl, CURLOPT_LOW_SPEED_LIMIT, 1024L);
  curl_easy_setopt(curl, CURLOPT_LOW_SPEED_TIME, 60L);
  curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
  curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, on_body);
  curl_easy_setopt(curl, CURLOPT_WRITEDATA, sink);
  curl_easy_setopt(curl, CURLOPT_ERRORBUFFER, errbuf);
  if (verify) {
    struct curl_blob blob = {(void *)ca_bundle, ca_bundle_len, CURL_BLOB_NOCOPY};
    curl_easy_setopt(curl, CURLOPT_CAINFO_BLOB, &blob);
  } else {
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 0L);
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 0L);
  }

  *code = curl_easy_perform(curl);
  long status = -1;
  next[0] = 0;
  if (*code == CURLE_OK) {
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
    char *location = NULL;
    if (status >= 300 && status < 400 && curl_easy_getinfo(curl, CURLINFO_REDIRECT_URL, &location) == CURLE_OK &&
        location)
      snprintf(next, next_size, "%s", location);
  } else {
    logf_("request failed (certificates %s, DNS %s): curl %d %s %s", verify ? "checked" : "not checked",
          own_dns ? "public" : "console", (int)*code, curl_easy_strerror(*code), errbuf);
  }
  curl_easy_cleanup(curl);
  curl_slist_free_all(resolve);
  return status;
}

/* GET url into the sink, following redirects (GitHub sends Release downloads to another host).
 * Certificates are checked first; if that check itself fails the request is repeated without it: what is
 * installed is still safe, because the package carries an HMAC made with the console's own key. */
static int http_get(const char *first_url, struct sink *sink) {
  char url[2048], next[2048];
  snprintf(url, sizeof url, "%s", first_url);
  for (int hop = 0; hop < 8; hop++) {
    long status = -1;
    int verify = 1, own_dns = 0;
    for (int attempt = 0; attempt < 4; attempt++) {
      if (sink->file) {
        rewind(sink->file);
        if (ftruncate(fileno(sink->file), 0) != 0)
          return -1;
      }
      sink->size = 0;
      sink->overflow = 0;
      CURLcode code;
      status = http_once(url, sink, verify, own_dns, next, sizeof next, &code);
      if (status >= 0)
        break;
      if (code == CURLE_COULDNT_RESOLVE_HOST && !own_dns)
        own_dns = 1;
      else if ((code == CURLE_PEER_FAILED_VERIFICATION || code == CURLE_SSL_CACERT_BADFILE ||
                code == CURLE_SSL_CERTPROBLEM || code == CURLE_SSL_CONNECT_ERROR) &&
               verify)
        verify = 0;
      else
        return -1;
    }
    if (status == 200)
      return sink->overflow ? -1 : 0;
    if (status >= 300 && status < 400 && next[0]) {
      snprintf(url, sizeof url, "%s", next);
      continue;
    }
    logf_("the server answered HTTP %ld for %s", status, url);
    return -1;
  }
  logf_("too many redirects");
  return -1;
}

/* ---------- the package ---------- */

static int sha256_file(const char *path, char hex[65], long long *size) {
  FILE *f = fopen(path, "rb");
  if (!f)
    return -1;
  EVP_MD_CTX *ctx = EVP_MD_CTX_new();
  EVP_DigestInit_ex(ctx, EVP_sha256(), NULL);
  static unsigned char buf[1 << 16];
  size_t n;
  long long total = 0;
  while ((n = fread(buf, 1, sizeof buf, f)) > 0) {
    EVP_DigestUpdate(ctx, buf, n);
    total += (long long)n;
  }
  unsigned char md[32];
  unsigned len = 0;
  EVP_DigestFinal_ex(ctx, md, &len);
  EVP_MD_CTX_free(ctx);
  fclose(f);
  bytes_to_hex(md, 32, hex);
  if (size)
    *size = total;
  return 0;
}

/* Checks the package's HMAC, then decrypts it into 'out_path'. Returns 0 and the zip's SHA-256. */
static int open_package(const char *pkg_path, const char *out_path, const unsigned char *aes_key,
                        const unsigned char *mac_key, char zip_sha[65], long long *zip_size) {
  static unsigned char buf[1 << 16], plain[(1 << 16) + 32];
  struct stat st;
  if (stat(pkg_path, &st) != 0 || st.st_size < 8 + 16 + 32) {
    logf_("the package is too small");
    return -1;
  }
  long long body = st.st_size - 32;
  FILE *in = fopen(pkg_path, "rb");
  if (!in)
    return -1;

  /* 1: the HMAC over everything before it */
  HMAC_CTX *mac = HMAC_CTX_new();
  HMAC_Init_ex(mac, mac_key, 32, EVP_sha256(), NULL);
  long long left = body;
  while (left > 0) {
    size_t want = left > (long long)sizeof buf ? sizeof buf : (size_t)left;
    if (fread(buf, 1, want, in) != want) {
      HMAC_CTX_free(mac);
      fclose(in);
      return -1;
    }
    HMAC_Update(mac, buf, want);
    left -= (long long)want;
  }
  unsigned char tag[32], expected[32];
  unsigned len = 0;
  HMAC_Final(mac, expected, &len);
  HMAC_CTX_free(mac);
  if (fread(tag, 1, 32, in) != 32 || CRYPTO_memcmp(tag, expected, 32) != 0) {
    logf_("the package's signature is wrong: damaged download, or made with another key");
    fclose(in);
    return -1;
  }

  /* 2: decrypt */
  unsigned char head[24];
  rewind(in);
  if (fread(head, 1, 24, in) != 24 || memcmp(head, "TB2PS5\x01\x00", 8) != 0) {
    logf_("the package has an unknown header");
    fclose(in);
    return -1;
  }
  FILE *out = fopen(out_path, "wb");
  if (!out) {
    logf_("can't create %s: %s", out_path, strerror(errno));
    fclose(in);
    return -1;
  }
  EVP_CIPHER_CTX *aes = EVP_CIPHER_CTX_new();
  EVP_MD_CTX *sha = EVP_MD_CTX_new();
  EVP_DecryptInit_ex(aes, EVP_aes_256_ctr(), NULL, aes_key, head + 8);
  EVP_DigestInit_ex(sha, EVP_sha256(), NULL);
  int ok = 1, produced = 0;
  left = body - 24;
  *zip_size = 0;
  while (ok && left > 0) {
    size_t want = left > (long long)sizeof buf ? sizeof buf : (size_t)left;
    if (fread(buf, 1, want, in) != want || !EVP_DecryptUpdate(aes, plain, &produced, buf, (int)want) ||
        fwrite(plain, 1, (size_t)produced, out) != (size_t)produced)
      ok = 0;
    EVP_DigestUpdate(sha, plain, (size_t)produced);
    *zip_size += produced;
    left -= (long long)want;
  }
  if (ok && EVP_DecryptFinal_ex(aes, plain, &produced) && produced > 0) {
    ok = fwrite(plain, 1, (size_t)produced, out) == (size_t)produced;
    EVP_DigestUpdate(sha, plain, (size_t)produced);
    *zip_size += produced;
  }
  unsigned char md[32];
  EVP_DigestFinal_ex(sha, md, &len);
  bytes_to_hex(md, 32, zip_sha);
  EVP_CIPHER_CTX_free(aes);
  EVP_MD_CTX_free(sha);
  fclose(in);
  if (fclose(out) != 0)
    ok = 0;
  if (!ok)
    logf_("the package could not be decrypted to %s: %s", out_path, strerror(errno));
  return ok ? 0 : -1;
}

/* Brings the build latest.json describes to DOWNLOAD_DIR/PS5SX3-<build>.zip. 0 when it is there. */
static int stage_update(const char *feed_url) {
  struct sink feed = {.limit = 65536};
  if (http_get(feed_url, &feed) != 0 || !feed.mem) {
    logf_("latest.json could not be read from %s", feed_url);
    free(feed.mem);
    return -1;
  }
  char build[32], zip_url[1024], sha[80], size_text[32];
  if (json_get(feed.mem, "build", build, sizeof build) || json_get(feed.mem, "zip", zip_url, sizeof zip_url) ||
      json_get(feed.mem, "sha256", sha, sizeof sha) || json_get(feed.mem, "size", size_text, sizeof size_text)) {
    logf_("latest.json lacks build, zip, sha256 or size");
    free(feed.mem);
    return -1;
  }
  free(feed.mem);
  long long size = atoll(size_text);
  if (strncmp(build, "TB", 2) != 0 || strspn(build + 2, "0123456789.") != strlen(build + 2) || strlen(build) < 3 ||
      strlen(sha) != 64 || strncmp(zip_url, "https://", 8) != 0 || size <= 0 || size > MAX_PACKAGE) {
    logf_("latest.json has an unexpected build name, address, SHA-256 or size");
    return -1;
  }
  logf_("latest build: %s, %lld bytes, sha256 %s", build, size, sha);

  char zip_path[256], part_path[256], pkg_path[256], pkg_url[1100], have[65];
  snprintf(zip_path, sizeof zip_path, DOWNLOAD_DIR "/PS5SX3-%s.zip", build);
  snprintf(part_path, sizeof part_path, "%s.tb2-part", zip_path);
  snprintf(pkg_path, sizeof pkg_path, "%s.tb2", zip_path);
  snprintf(pkg_url, sizeof pkg_url, "%s.tb2", zip_url);
  long long have_size = 0;
  if (sha256_file(zip_path, have, &have_size) == 0 && strcmp(have, sha) == 0) {
    logf_("%s is already there and matches", zip_path);
    return 0;
  }

  unsigned char *key_text = NULL, aes_key[32], mac_key[32];
  size_t key_len = 0;
  if (read_file(KEY_FILE, &key_text, &key_len, 4096) != 0 || key_len < 128 ||
      hex_to_bytes((char *)key_text, aes_key, 32) || hex_to_bytes((char *)key_text + 64, mac_key, 32)) {
    notify("PS5SX3 (fix The Bigs 2): falta la clave %s", KEY_FILE);
    free(key_text);
    return -1;
  }
  free(key_text);

  mkdir(WORK_DIR, 0777);
  mkdir(DOWNLOAD_DIR, 0777);
  notify("PS5SX3 %s con el fix de The Bigs 2: descargando (%lld MB)...", build, size / (1024 * 1024));
  struct sink pkg = {.limit = (size_t)size + 4096};
  pkg.file = fopen(pkg_path, "wb+");
  if (!pkg.file) {
    logf_("can't create %s: %s", pkg_path, strerror(errno));
    return -1;
  }
  int got = http_get(pkg_url, &pkg);
  if (fclose(pkg.file) != 0)
    got = -1;
  if (got != 0) {
    notify("PS5SX3 (fix The Bigs 2): no se pudo descargar la version %s", build);
    unlink(pkg_path);
    return -1;
  }
  logf_("downloaded %zu bytes from %s", pkg.size, pkg_url);

  char zip_sha[65];
  long long zip_size = 0;
  int result = open_package(pkg_path, part_path, aes_key, mac_key, zip_sha, &zip_size);
  unlink(pkg_path);
  if (result == 0 && (strcmp(zip_sha, sha) != 0 || zip_size != size)) {
    logf_("the decrypted zip is %lld bytes, sha256 %s: not what latest.json gives", zip_size, zip_sha);
    result = -1;
  }
  if (result == 0 && rename(part_path, zip_path) != 0) {
    logf_("can't put %s in place: %s", zip_path, strerror(errno));
    result = -1;
  }
  if (result != 0) {
    unlink(part_path);
    notify("PS5SX3 (fix The Bigs 2): la descarga de %s no paso la comprobacion. No se cambio nada.", build);
    return -1;
  }
  logf_("%s is ready (signature and SHA-256 checked)", zip_path);
  return 0;
}

/* ---------- handing over to the real installer ---------- */

static int block_is_blank(const unsigned char *block) {
  for (int i = MARKER_LEN; i < BLOCK_SIZE; i++)
    if (block[i])
      return 0;
  return 1;
}

static int start_stock_installer(const char *title_dir, const unsigned char *my_block) {
  char path[512];
  snprintf(path, sizeof path, "%s/%s", title_dir, STOCK_NAME);
  unsigned char *elf = NULL;
  size_t size = 0;
  if (read_file(path, &elf, &size, 64u * 1024 * 1024) != 0 || size < BLOCK_SIZE || memcmp(elf, "\x7f" "ELF", 4) != 0) {
    logf_("the real installer can't be read: %s", path);
    free(elf);
    return -1;
  }
  /* The same search PS5SX3 does: the one block that is still blank */
  long found = -1;
  for (size_t i = 0; i + BLOCK_SIZE <= size; i++) {
    if (elf[i] == 'P' && memcmp(elf + i, my_block, MARKER_LEN) == 0 && block_is_blank(elf + i)) {
      if (found >= 0) {
        found = -2;
        break;
      }
      found = (long)i;
    }
  }
  if (found < 0) {
    logf_("the real installer has %s launch block", found == -1 ? "no" : "more than one");
    free(elf);
    return -1;
  }
  memcpy(elf + found, my_block, BLOCK_SIZE);

  int sock = socket(AF_INET, SOCK_STREAM, 0);
  struct sockaddr_in addr;
  memset(&addr, 0, sizeof addr);
  addr.sin_family = AF_INET;
  addr.sin_port = htons(ELF_LOADER_PORT);
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  if (sock < 0 || connect(sock, (struct sockaddr *)&addr, sizeof addr) != 0) {
    logf_("no ELF loader on 127.0.0.1:%d: %s", ELF_LOADER_PORT, strerror(errno));
    if (sock >= 0)
      close(sock);
    free(elf);
    return -1;
  }
  size_t sent = 0;
  while (sent < size) {
    ssize_t n = send(sock, elf + sent, size - sent, 0);
    if (n <= 0) {
      logf_("the ELF loader stopped after %zu of %zu bytes: %s", sent, size, strerror(errno));
      close(sock);
      free(elf);
      return -1;
    }
    sent += (size_t)n;
  }
  close(sock);
  free(elf);
  logf_("the real installer (%zu bytes) went to the ELF loader", size);
  return 0;
}

int main(void) {
  unsigned char block[BLOCK_SIZE];
  for (int i = 0; i < BLOCK_SIZE; i++)
    block[i] = launch_block[i];

  mkdir(WORK_DIR, 0777);
  logf_("---- tb2-fetch started (pid %d)", (int)getpid());

  char title_dir[256] = DEFAULT_TITLE_DIR;
  unsigned char *link = NULL;
  size_t link_len = 0;
  if (read_file(MOUNT_LINK, &link, &link_len, 200) == 0 && link_len > 1 && link[0] == '/') {
    while (link_len > 0 && (link[link_len - 1] == '\n' || link[link_len - 1] == '\r' || link[link_len - 1] == '/'))
      link[--link_len] = 0;
    snprintf(title_dir, sizeof title_dir, "%s", (char *)link);
  }
  free(link);

  /* Where latest.json is: PS5SX3 passes the address it used; started by hand, the installer's own file */
  char feed_url[0x200] = "";
  if (memcmp(block + 0x20, "PXI1", 4) == 0 && block[BLOCK_URL])
    snprintf(feed_url, sizeof feed_url, "%.*s", 0x1ff, (char *)block + BLOCK_URL);
  if (!feed_url[0]) {
    unsigned char *text = NULL;
    size_t len = 0;
    if (read_file(WORK_DIR "/manifest-url.txt", &text, &len, 2048) == 0) {
      size_t n = strcspn((char *)text, "\r\n \t");
      if (n < sizeof feed_url && strncmp((char *)text, "https://", 8) == 0)
        snprintf(feed_url, sizeof feed_url, "%.*s", (int)n, (char *)text);
    }
    free(text);
  }

  if (feed_url[0]) {
    logf_("latest.json: %s", feed_url);
    curl_global_init(CURL_GLOBAL_DEFAULT);
    int staged = stage_update(feed_url);
    curl_global_cleanup();
    logf_(staged == 0 ? "the update is ready for the installer" : "the update was not prepared; the installer will say so");
  } else {
    logf_("no address of our own: the real installer runs as it always did");
  }

  if (start_stock_installer(title_dir, block) != 0) {
    notify("PS5SX3 (fix The Bigs 2): no se pudo iniciar el instalador original. Abre PS5SX3 desde el menu.");
    return 1;
  }
  logf_("---- done");
  return 0;
}
