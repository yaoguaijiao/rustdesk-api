#!/usr/bin/env python3
"""CI private package guard; never print credentials or response bodies."""
import json, os, re, sys, urllib.error, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None
def request(path, missing_ok=False):
    headers={"Authorization":"Bearer "+os.environ["GH_TOKEN"],
             "Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            urllib.request.Request("https://api.github.com"+path,headers=headers),timeout=30) as r:
            raw=r.read(1024*1024+1)
            if len(raw)>1024*1024: raise RuntimeError("metadata_too_large")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        if missing_ok and e.code==404: return None
        raise RuntimeError("package_metadata_http_"+str(e.code)) from None
def main():
    owner=os.environ["GITHUB_REPOSITORY_OWNER"].lower()
    version=os.environ["RELEASE_VERSION"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*",owner):raise RuntimeError("invalid_owner")
    path="/users/"+owner+"/packages/container/rustdesk-server-s6"
    before=os.environ["GUARD_PHASE"]=="before"
    p=request(path,missing_ok=before)
    if p is not None and p.get("visibility")!="private":
        raise RuntimeError("existing_package_is_not_private")
    if before:
        if p:
            for item in request(path+"/versions?per_page=100"):
                if version in item.get("metadata",{}).get("container",{}).get("tags",[]):
                    raise RuntimeError("fixed_version_exists_choose_a_new_version")
        print("PASS private-package preflight")
        return
    if p is None:raise RuntimeError("published_package_missing")
    digest=os.environ["IMAGE_DIGEST"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}",digest):raise RuntimeError("invalid_digest")
    record={"version":version,"image":os.environ["REGISTRY_IMAGE"],"digest":digest,
            "visibility":"private","platform":"linux/amd64",
            "api_revision":os.environ["GITHUB_SHA"],"server_revision":os.environ["SERVER_REF"],
            "frontend_revision":os.environ["FRONTEND_REF"],
            "run_url":"https://github.com/"+os.environ["GITHUB_REPOSITORY"]+
                      "/actions/runs/"+os.environ["GITHUB_RUN_ID"]}
    with open("release-record.json","w",encoding="utf-8") as f:json.dump(record,f,indent=2)
    print("PASS published package visibility is private")
if __name__=="__main__":
    try:main()
    except Exception as e:
        print(str(e) if isinstance(e,RuntimeError) else type(e).__name__,file=sys.stderr)
        sys.exit(1)
