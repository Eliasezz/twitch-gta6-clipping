"""Private zip export for a finished 9:16 video and ready-to-use social copy."""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

from .publishing import publication_package


class ExportError(RuntimeError):
    pass


def export_bundle(job_id, job_dir, status):
    """Return zip path; output always includes the completed MP4."""
    folder = Path(job_dir)
    mp4 = folder / "vertical.mp4"
    if status.get("state") != "done" or not mp4.is_file():
        raise ExportError("Edited video is not finished")
    if mp4.stat().st_size < 1024 or mp4.is_symlink():
        raise ExportError("Rendered MP4 is invalid")
    from .media_integrity import sha256_file
    clips = (status.get("result") or {}).get("clips")
    clips = clips if clips else [status.get("result") or {}]
    for index, detail in enumerate(clips, 1):
        name = "vertical.mp4" if index == 1 else f"vertical-{index}.mp4"
        item = folder / name
        expected = (detail.get("output_validation") or {}).get("output_sha256")
        if (detail.get("job_id") not in (None, job_id)
                or detail.get("file") not in (None, name)
                or not item.is_file() or item.is_symlink()
                or item.resolve().parent != folder.resolve()):
            raise ExportError("Clip missing or assigned to another job")
        if expected and sha256_file(item) != expected:
            raise ExportError("Clip changed since output verification")
    archive = folder / "ready-to-publish.zip"
    cover = folder / "cover.jpg"
    package = publication_package(job_id, status)
    demo = bool(status.get("demo")) or status.get("authorized") is not True
    if demo:
        package.update({
            "title": "SYNTHETIC TEST - DO NOT PUBLISH",
            "tiktok_caption": "Technical demo only. Not intended for publication.",
            "youtube_title": "SYNTHETIC TEST - DO NOT PUBLISH",
            "youtube_description": "Synthetic software test, not real GTA VI footage.",
            "ready": False, "status": "demo_not_publishable",
        })
    if not archive.is_file() or archive.stat().st_mtime < mp4.stat().st_mtime:
        if not cover.is_file() and shutil.which("ffmpeg"):
            result = subprocess.run([
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-y", "-ss", "1", "-i", str(mp4), "-frames:v", "1",
                "-q:v", "3", str(cover),
            ], capture_output=True, text=True, timeout=45)
            if result.returncode != 0:
                cover.unlink(missing_ok=True)
        instructions = (
            "GTA VI / CLIP PRÊT POUR PUBLICATION\n\n"
            "1. Décompressez ce ZIP dans votre dossier Clips GTA VI sur le Mac.\n"
            "2. Regardez la vidéo et vérifiez le son, les sous-titres et le cadrage.\n"
            "3. Vérifiez les droits du créateur, la musique et les règles d'originalité.\n"
            "4. Ouvrez https://app.metricool.com/ et le calendrier de ACTU GTA VI.\n"
            "5. Importez montage_9x16.mp4 depuis votre ordinateur.\n"
            "6. Copiez les textes fournis, choisissez TikTok et YouTube Shorts, "
            "puis programmez ou publiez.\n"
            "7. Pour YouTube, indiquez correctement si le contenu est destiné aux enfants.\n"
            "8. Une durée de 65 secondes ne garantit pas la monétisation.\n"
        )
        if demo:
            instructions = (
                "DÉMONSTRATION SYNTHÉTIQUE — NE PAS PUBLIER SUR TIKTOK/YOUTUBE.\n\n"
                + instructions
            )
        dirname = "GTA_VI_" + job_id[:8] + "/"
        # Concurrent /export and /export-link requests previously used
        # the same temp filename and raced during os.replace(), causing
        # FileNotFoundError HTTP 500 even though the MP4 was finished.
        # One temporary filename per request makes both atomic and safe.
        with tempfile.NamedTemporaryFile(
            prefix="publish-", suffix=".zip.tmp", dir=folder,
            delete=False
        ) as temporary:
            temp_zip = Path(temporary.name)
        try:
            with ZipFile(temp_zip, "w", allowZip64=True) as z:
                z.write(mp4, dirname + "montage_9x16.mp4", compress_type=ZIP_STORED)
                for index, clip in enumerate((status.get("result") or {}).get("clips", [])[1:], 2):
                    z.write(folder / f"vertical-{index}.mp4", dirname + f"montage_{index}_9x16.mp4", compress_type=ZIP_STORED)
                if cover.is_file():
                    z.write(cover, dirname + "couverture.jpg", compress_type=ZIP_STORED)
                z.writestr(dirname + "tiktok_description.txt",
                           package["tiktok_caption"], compress_type=ZIP_DEFLATED)
                z.writestr(dirname + "youtube_titre.txt",
                           package["youtube_title"], compress_type=ZIP_DEFLATED)
                z.writestr(dirname + "youtube_description.txt",
                           package["youtube_description"], compress_type=ZIP_DEFLATED)
                z.writestr(dirname + "INSTRUCTIONS_FR.txt",
                           instructions, compress_type=ZIP_DEFLATED)
                z.writestr(dirname + "publication.json",
                           json.dumps(package, indent=2, ensure_ascii=False),
                           compress_type=ZIP_DEFLATED)
            temp_zip.replace(archive)
        finally:
            temp_zip.unlink(missing_ok=True)
    return archive
