import configparser
import os
from pathlib import Path

confs_dir = Path("confs")
repos_dir = confs_dir / "repos"
repos_dir.mkdir(exist_ok=True)

cp = configparser.ConfigParser()
# Preserve case
cp.optionxform = str
cp.read(confs_dir / "default.conf")

repos = []
una_section = {}

for section in cp.sections():
    if section == "una":
        una_section = dict(cp[section])
        continue
    
    # Write to individual .repo file
    repo_file = repos_dir / f"{section}.repo"
    with open(repo_file, "w") as f:
        f.write(f"[{section}]\n")
        for k, v in cp[section].items():
            f.write(f"{k} = {v}\n")
    repos.append(f"confs/repos/{section}.repo")

# Also let's extract the commented out rust part manually if needed, 
# but it's simpler to just copy it or ignore it. For now configparser ignores comments.

# Write new default.conf
with open(confs_dir / "default.conf", "w") as f:
    f.write("[una]\n")
    f.write(f"repos = {', '.join(repos)}\n")
    # write the existing una section keys
    for k, v in una_section.items():
        f.write(f"{k} = {v}\n")

print("Done")
