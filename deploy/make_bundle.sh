#!/usr/bin/env bash
# make_bundle.sh —— 把交付物打成「解压即装」的单一压缩包
#   产物：medrec-deploy-<版本>-<构建>-<目标机>.tar.gz
#         默认 medrec-deploy-1.0.0-r52-2xl40.tar.gz（解压出同名目录，进去 bash install.sh 就装好环境）
#   改名：RELEASE_VERSION / RELEASE_BUILD / RELEASE_TARGET 三个环境变量，或 BUNDLE_OUT 直接给全路径。
# ★ 两个包体本身已是 gzip，这里用 pigz -1：再压一遍省不了几个字节，纯耗时间。
set -euo pipefail

PKG="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
VERSION="${RELEASE_VERSION:-1.0.0}"
BUILD="${RELEASE_BUILD:-r52}"
TARGET="${RELEASE_TARGET:-2xl40}"
NAME="medrec-deploy-${VERSION}-${BUILD}-${TARGET}"
STAGE="$PKG/work/bundle-stage"
OUT="${BUNDLE_OUT:-$PKG/${NAME}.tar.gz}"
D="$STAGE/$NAME"
case "$NAME" in
  *[!A-Za-z0-9._-]*) echo "[FAIL] 发布名含非法字符：$NAME"; exit 1 ;;
esac

mkdir -p "$D/env"
cd "$PKG"
cp -a install.sh tune.sh start-all.sh stop-all.sh health.sh pkg_tool.py DELIVERY.md QUICKSTART.txt "$D/"
cp -a medrec-code-round52.tar.gz medrec-code-round52.manifest.json \
      medrec-assets-round52.tar.gz medrec-assets-round52.manifest.json "$D/"
cp -a env/README.md "$D/env/"
cp -a env/install-vl-env.txt env/install-ocr-env.txt env/install-qwen-env.txt "$D/env/"

chmod 700 "$D" "$D/env"
chmod 700 "$D"/*.sh
chmod 600 "$D"/*.txt "$D"/*.md "$D"/*.json "$D"/*.tar.gz "$D"/env/*

# ★ 发布身份写进包内的 VERSION 文件：install.sh 开头会打印它，出问题时报的是版本号不是「那个 round 几几的包」
# ★ 取 install.sh 里的默认档位/版本：写法是 VAR="${VAR:-值}"，捕获组后一个字符是 } 不是引号。
#   旧 sed 按引号收尾 ⇒ 取回空串 ⇒ 包内 VERSION 静默写成 strata_model=（查版本查到的是空的）。
kv_from_install() { sed -n "s/^$1=.*:-\([^}]*\)}.*/\1/p" "$PKG/install.sh" | head -1; }
sv="$(kv_from_install STRATA_VERSION)"
sc="$(kv_from_install STRATA_COMMIT)"
sm="$(kv_from_install STRATA_MODEL)"
cs="$(python3 "$PKG/pkg_tool.py" field "$PKG/medrec-code-round52.manifest.json" archive_sha256 | cut -c1-12)"
as="$(python3 "$PKG/pkg_tool.py" field "$PKG/medrec-assets-round52.manifest.json" archive_sha256 | cut -c1-12)"
if [ -z "$sv" ] || [ -z "$sc" ] || [ -z "$sm" ] || [ -z "$cs" ] || [ -z "$as" ]; then
  echo "[FAIL] install.sh 里没取到 STRATA_VERSION/COMMIT/MODEL，或 manifest 取不到 sha256（sv=$sv sc=${sc:0:12} sm=$sm cs=$cs as=$as）—— 停，不打出身份为空的包"
  exit 1
fi
{
  echo "release=$NAME"
  echo "version=$VERSION"
  echo "build=$BUILD"
  echo "target=$TARGET"
  echo "built=$(date -Iseconds)"
  echo "strata_repo=https://github.com/Niko1221/Strata.git"
  echo "strata_pin=$sv / $sc"
  echo "strata_model=$sm"
  echo "code_sha256_12=$cs"
  echo "assets_sha256_12=$as"
} > "$D/VERSION"
chmod 600 "$D/VERSION"
echo "[OK] VERSION：$NAME（strata $sv / $sm）"

echo "[..] 打包 $D → $OUT"
tar -I "pigz -1" -cf "$OUT" -C "$STAGE" "$NAME"

echo ""
echo "===== 打包后自查 ====="
list="$PKG/work/bundle_listing.txt"
tar -tzf "$OUT" > "$list"
echo "条目数：$(grep -c "." "$list")"
bad_hits="$(grep -cE '(^|/)(run|workspace|logs|__pycache__|\.git)(/|$)|\.bak$|~$|fakeroot' "$list" || true)"
echo "禁运内容命中：$bad_hits（要求 0）"
[ "$bad_hits" = 0 ] || { echo "[FAIL] 包里混进了不该打的东西，见 $list"; exit 1; }
for need in "$NAME/install.sh" "$NAME/tune.sh" "$NAME/start-all.sh" "$NAME/stop-all.sh" "$NAME/health.sh" \
            "$NAME/pkg_tool.py" "$NAME/DELIVERY.md" "$NAME/QUICKSTART.txt" "$NAME/VERSION" \
            "$NAME/medrec-code-round52.tar.gz" "$NAME/medrec-code-round52.manifest.json" \
            "$NAME/medrec-assets-round52.tar.gz" "$NAME/medrec-assets-round52.manifest.json" \
            "$NAME/env/install-vl-env.txt" "$NAME/env/install-ocr-env.txt" "$NAME/env/install-qwen-env.txt"; do
  grep -qx "$need" "$list" || { echo "[FAIL] 缺成员：$need"; exit 1; }
done
echo "[OK] 16 个必需成员都在"
ls -l "$OUT" | awk '{print "大小：" $5 " 字节"}'
sha256sum "$OUT"
echo "解包后落点：$STAGE/$NAME（install.sh 在该目录里跑，两个 tar.gz 必须在它同级）"

# ★ 权威 sha 落在【包外】sidecar：DELIVERY.md 在包内，改它一个字节 sha 就变，
#   所以文档里写死的 sha 只能是快照，核对一律用这个文件。
sha256sum "$OUT" > "$OUT.sha256"
chmod 600 "$OUT.sha256"
echo "sidecar：$OUT.sha256"

# ★ 自查结果同时落盘，别只写 stdout（make_bundle3 那次 stdout 丢了，见 DELIVERY §5-13）
{
  echo "archive=$OUT"
  echo "bytes=$(stat -c %s "$OUT")"
  echo "entries=$(grep -c . "$list")"
  echo "files=$(grep -v "/$" "$list" | wc -l)"
  echo "banned_hits=$bad_hits"
  echo "required_members=16"
  echo "sha256=$(sha256sum "$OUT" | cut -d" " -f1)"
} > "$PKG/work/bundle_selfcheck.txt"
echo "自查落盘：$PKG/work/bundle_selfcheck.txt"
