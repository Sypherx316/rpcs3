# PS5SX3 + fix de The Bigs 2 (automático)

Todo lo de PS5SX3 en este fork: el workflow [`ps5sx3.yml`](../workflows/ps5sx3.yml) usa esta carpeta.

Cada 6 horas un workflow mira si el desarrollador de PS5SX3 publicó una versión nueva. Si la hay:

1. baja el zip oficial y comprueba su tamaño y SHA-256;
2. aplica al `eboot.bin` el fix del público de The Bigs 2 (RPCS3 #17741) con `autopatch.py`;
3. vuelve a armar el zip sin tocar ningún otro archivo;
4. pone en el zip el instalador propio (`installer/`, se compila en el mismo workflow) como
   `PS5SX3-Installer.elf` y deja el original al lado como `PS5SX3-Installer-stock.elf`;
5. lo sube **cifrado** (`PS5SX3-<versión>-thebigs2-fix.zip.tb2`) al Release más nuevo de este fork, donde están
   también RPCS3 para Windows, Linux y PS5, y actualiza `ps5sx3/latest.json`.

Cuando sale un Release nuevo de RPCS3, el mismo workflow vuelve a poner ahí el paquete de PS5SX3. En cada Release
solo queda la versión más nueva de PS5SX3.

## De dónde se actualiza la consola

De los [Releases de este fork](https://github.com/Sypherx316/rpcs3/releases). El aviso que lee la consola es
`ps5sx3/latest.json` (en la raíz del repositorio):
`https://raw.githubusercontent.com/Sypherx316/rpcs3/master/ps5sx3/latest.json`.
Su `zip` apunta al paquete del Release más nuevo.

Hasta noviembre de 2026 el paquete se compilaba en `Sypherx316/ps5sx3-thebigs2` (archivado) y se copiaba aquí; la
dirección de la consola es la misma, así que no hubo que tocar nada en la PS5.

## En la consola

PS5SX3 lee `rpcs3/config/update-url.txt` (dentro de su carpeta) y, si tiene una dirección `https://`, busca
ahí las actualizaciones. Con la dirección de `ps5sx3/latest.json` de `rpcs3` avisa de cada versión nueva igual que siempre.
Al aceptar, lanza `PS5SX3-Installer.elf`, que aquí es `installer/main.c`:

1. baja `latest.json` y el paquete `.tb2`;
2. comprueba la firma HMAC con `/data/rpcs3/tb2-update.key` y lo descifra;
3. deja el zip en `/data/PS5SX3-Installer/download/` y comprueba su SHA-256;
4. lanza el instalador original, que reutiliza ese zip, instala con sus respaldos y vuelve a abrir PS5SX3.

Si algo falla, no se toca nada de PS5SX3. Registro: `/data/PS5SX3-Installer/tb2-fetch.log`.

Para volver al original: borrar `rpcs3/config/update-url.txt` y renombrar `PS5SX3-Installer-stock.elf`
a `PS5SX3-Installer.elf`.

PS5SX3 no tiene código fuente público, así que no se compila nada: `autopatch.py` localiza la función
`calculate_required_range` dentro del binario y la compara instrucción por instrucción con una versión de
referencia. Hay dos, porque a partir de la TB24 el desarrollador compila sin *frame pointer* (las variables
van por `rsp` en vez de `rbp` y cambian los registros), así que el parche tiene dos versiones:
`reference_tb20.json` (TB18.7 a TB22.1) y `reference_tb24.json` (TB24.1 en adelante). Si una versión nueva no
coincide con ninguna, **no se publica nada**, el workflow falla (GitHub
avisa por correo) y la consola se queda en la versión anterior.

| Archivo | Para qué |
|---|---|
| `autopatch.py` | Aplica el fix a un `eboot.bin` |
| `reference_tb20.json` | La función tal como está en la TB20, para reconocer las versiones con *frame pointer* |
| `reference_tb24.json` | La función tal como está en la TB24.1, para reconocer las versiones sin *frame pointer* |
| `make_reference.py` | Regenera una referencia desde el `eboot.bin` original de la TB20 o de la TB24.1 |
| `build_update.py` | Baja la versión del desarrollador, la arregla, la cifra y escribe `latest.json` |
| `compare.py` | Compara un paquete recompilado con el publicado (lo usa el modo `dry_run`) |
| `setup_ps5.ps1` | Prepara la consola una sola vez: instalador propio y dirección del aviso |
| `../../ps5sx3/latest.json` | Lo que lee la consola: versión, tamaño y SHA-256 del zip arreglado |
| `installer/` | El instalador propio para la consola (C, ps5-payload-sdk) |

Secreto necesario: `PS5_KEY` (128 caracteres hex: clave AES-256 + clave HMAC), la misma que tiene la consola y la
misma que cifra RPCS3 para PS5.

Para probar un cambio sin publicar nada: **Run workflow** con `dry_run`, o cualquier push que cambie esta carpeta.
Compila la versión actual del desarrollador y la compara con el paquete que tiene la consola.

Probado con las versiones TB18.7, TB18.9, TB19, TB19.6, TB19.7, TB20, TB21, TB22, TB22.1 y TB24.1. El resultado para la TB20 es idéntico
byte a byte al parche hecho a mano y probado en la consola.

## Créditos

Esta carpeta no es parte de PS5SX3 ni está afiliado a sus autores; solo aplica un arreglo a sus versiones
publicadas para uso propio. Todo el mérito del emulador es de quienes lo hacen:

- **PS5SX3**: Sword ([@sword_pdf](https://x.com/sword_pdf), [Swordpdf en GitHub](https://github.com/Swordpdf)).
  Las versiones y su servidor de actualizaciones son suyos.
- **RPCS3**: el [equipo de RPCS3](https://github.com/RPCS3/rpcs3), en el que se basa PS5SX3.
- **El arreglo de The Bigs 2**: [digant73](https://github.com/digant73), commit
  [5cdab28](https://github.com/digant73/rpcs3/commit/5cdab280718aea5ea97fb8ec646cce4e23abb98f) para el issue
  [RPCS3 #17741](https://github.com/RPCS3/rpcs3/issues/17741), reportado por Virtua64.
- **ps5-payload-sdk**: [ps5-payload-dev](https://github.com/ps5-payload-dev/sdk), con el que se compila el
  instalador de `installer/` (usa curl y OpenSSL de sus paquetes).
