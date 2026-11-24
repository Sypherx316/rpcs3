# Builds PS5SX3-Installer.elf (the update fetcher) with ps5-payload-sdk and its curl/openssl packages.
ifdef PS5_PAYLOAD_SDK
    include $(PS5_PAYLOAD_SDK)/toolchain/prospero.mk
else
    $(error PS5_PAYLOAD_SDK is undefined)
endif

CA_BUNDLE ?= /etc/ssl/certs/ca-certificates.crt

CFLAGS := -O2 -Wall -Werror -Wno-deprecated-declarations -Wno-unreachable-code-generic-assoc
CFLAGS += `$(PS5_PAYLOAD_SDK)/bin/prospero-curl-config --cflags`
LDADD  := `$(PS5_PAYLOAD_SDK)/bin/prospero-curl-config --libs`

ELF := PS5SX3-Installer.elf

all: $(ELF)

ca_bundle.c: $(CA_BUNDLE) gen_ca.py
	python3 gen_ca.py $(CA_BUNDLE) $@

$(ELF): main.c ca_bundle.c
	$(CC) $(CFLAGS) -o $@ $^ $(LDADD)

clean:
	rm -f $(ELF) ca_bundle.c
