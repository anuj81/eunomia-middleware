"""Seed OpenMetadata with Phase-D identity + tag-based authorization (Path A3).

Idempotent. Run after Keycloak is up and OM is in custom-oidc mode.

Authorization model:
    • Tag classification `eunomia-access` with one tag per business role.
    • Each view is tagged with the role tags authorized to see it.
        finance_daily_revenue_view              [finance-user]
        finance_customer_payment_history_view   [finance-user, external-auditor]
        marketing_customer_ltv_view             [marketing-lead]
        marketing_regional_performance_view     [marketing-lead, agency-partner]
    • One policy per role with a single deny rule:
        deny  ViewAll,ViewBasic on table  if  !matchAnyTag('eunomia-access.<role>')
    • One role per business role, attached to that policy.
    • One Group team per business role, holding the user and defaultRoles=[role].
    • Per OM evaluation: deny beats OrganizationPolicy's blanket allow.
    • om.admin is in `adminPrincipals` → bypasses policy entirely.

Why tag-based, not team-ownership:
    OM 1.12 enforces "owner = single Group team" on entities, and Groups
    cannot parent Groups. So a view authorized to two roles can't be owned
    by a single Group that covers both. Tagging works around this elegantly
    and the policy syntax is clean.

Run:
    python seed_om_policies.py
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, List, Optional

import requests

OM_URL = "http://localhost:8585/api/v1"
KEYCLOAK_URL = "http://localhost:8080/realms/eunomia"
SCHEMA_FQN = "zenith_mysql.zenith_corp_eunomia.zenith_corp_eunomia"

# (keycloak_username, om_name, email, displayName)
USERS = [
    ("finance.alice",   "finance.alice",   "finance.alice@open-metadata.org",   "Alice Finance"),
    ("auditor.bob",     "auditor.bob",     "auditor.bob@open-metadata.org",     "Bob Auditor"),
    ("marketing.carol", "marketing.carol", "marketing.carol@open-metadata.org", "Carol Marketing"),
    ("agency.dave",     "agency.dave",     "agency.dave@open-metadata.org",     "Dave Agency"),
    ("om.admin",        "om.admin",        "om.admin@open-metadata.org",        "OM Admin"),
    # Service account for the RAG indexer — its Keycloak token now carries
    # preferred_username and email matching this user.
    ("service-account-eunomia-rag-indexer",
     "service-account-eunomia-rag-indexer",
     "service-account-eunomia-rag-indexer@open-metadata.org",
     "RAG Indexer service account"),
]

TAG_CLASSIFICATION = "eunomia-access"
# Business-role identity (short name used in tag, policy, role, team labels).
ROLES = ["finance-user", "external-auditor", "marketing-lead", "agency-partner"]

# Each business role gets one Team with the matching user assigned.
TEAM_MEMBERS = {
    "finance-user":     ["finance.alice"],
    "external-auditor": ["auditor.bob"],
    "marketing-lead":   ["marketing.carol"],
    "agency-partner":   ["agency.dave"],
}

# Access matrix: view → list of role short-names allowed to see it.
VIEW_ACCESS = {
    "finance_daily_revenue_view":            ["finance-user"],
    "finance_customer_payment_history_view": ["finance-user", "external-auditor"],
    "marketing_customer_ltv_view":           ["marketing-lead"],
    "marketing_regional_performance_view":   ["marketing-lead", "agency-partner"],
}


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #


def keycloak_admin_token() -> str:
    for attempt in range(30):
        try:
            r = requests.post(
                f"{KEYCLOAK_URL}/protocol/openid-connect/token",
                data={"client_id": "eunomia-cli", "grant_type": "password",
                      "username": "om.admin", "password": "test"},
                timeout=5,
            )
            if r.status_code == 200:
                return r.json()["access_token"]
        except Exception as e:
            print(f"  attempt {attempt}: {e}")
        time.sleep(2)
    raise RuntimeError("Could not log into Keycloak as om.admin")


def om_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return s


def put(s, path, body, label):
    r = s.put(f"{OM_URL}{path}", data=json.dumps(body))
    if r.status_code in (200, 201):
        print(f"  ✓ {label} ({r.status_code})")
        return r.json()
    print(f"  ✗ {label}: HTTP {r.status_code} — {r.text[:200]}")
    return None


def get_by_name(s, path):
    r = s.get(f"{OM_URL}{path}")
    return r.json() if r.status_code == 200 else None


def patch_entity(s, path, ops, label):
    headers = {"Content-Type": "application/json-patch+json", "Accept": "application/json"}
    r = s.patch(f"{OM_URL}{path}", data=json.dumps(ops), headers=headers)
    if r.status_code in (200, 201):
        print(f"  ✓ {label} ({r.status_code})")
        return r.json()
    print(f"  ✗ {label}: HTTP {r.status_code} — {r.text[:200]}")
    return None


# --------------------------------------------------------------------------- #
# steps                                                                       #
# --------------------------------------------------------------------------- #


_ADMIN_OM_USERS = {"om.admin", "service-account-eunomia-rag-indexer"}


def seed_users(s) -> Dict[str, str]:
    print("--- Users ---")
    ids: Dict[str, str] = {}
    for _, name, email, display in USERS:
        is_admin = name in _ADMIN_OM_USERS
        body = {"name": name, "email": email, "displayName": display,
                "isBot": False, "isAdmin": is_admin}
        suffix = " (admin)" if is_admin else ""
        r = put(s, "/users", body, f"user {name}{suffix}")
        if r:
            ids[name] = r["id"]
        else:
            existing = get_by_name(s, f"/users/name/{name}")
            if existing:
                ids[name] = existing["id"]
    return ids


def seed_classification_and_tags(s):
    print("--- Classification + tags ---")
    # Classification (tag category)
    body = {
        "name": TAG_CLASSIFICATION,
        "description": "Authorization tags consumed by Eunomia's deny-by-tag policies.",
        "provider": "user",
        "mutuallyExclusive": False,
    }
    r = put(s, "/classifications", body, f"classification {TAG_CLASSIFICATION}")
    if not r:
        existing = get_by_name(s, f"/classifications/name/{TAG_CLASSIFICATION}")
        if not existing:
            return False
    # Tags under the classification
    for role in ROLES:
        body = {
            "name": role,
            "description": f"Marks an entity as accessible to the '{role}' role.",
            "classification": TAG_CLASSIFICATION,
        }
        put(s, "/tags", body, f"tag {TAG_CLASSIFICATION}.{role}")
    return True


def seed_policies(s) -> Dict[str, str]:
    """One policy per role: deny ViewAll on tables not bearing this role's tag."""
    print("--- Policies ---")
    out: Dict[str, str] = {}
    for role in ROLES:
        tag_fqn = f"{TAG_CLASSIFICATION}.{role}"
        body = {
            "name": f"policy-{role}",
            "description": f"Deny ViewAll/ViewBasic on tables that lack tag {tag_fqn}.",
            "rules": [
                {
                    "name": f"deny-untagged-tables-for-{role}",
                    "effect": "deny",
                    "operations": ["ViewAll", "ViewBasic"],
                    "resources": ["table"],
                    "condition": f"!matchAnyTag('{tag_fqn}')",
                }
            ],
        }
        r = put(s, "/policies", body, f"policy policy-{role}")
        if r:
            out[role] = r["id"]
        else:
            ex = get_by_name(s, f"/policies/name/policy-{role}")
            if ex:
                out[role] = ex["id"]
    return out


def seed_roles(s, policy_ids: Dict[str, str]) -> Dict[str, str]:
    print("--- Roles ---")
    out: Dict[str, str] = {}
    for role in ROLES:
        body = {
            "name": f"role-{role}",
            "displayName": f"Role: {role}",
            "description": f"Carries policy-{role} (deny tables lacking the {role} tag).",
            "policies": [f"policy-{role}"],
        }
        r = put(s, "/roles", body, f"role role-{role}")
        if r:
            out[role] = r["id"]
        else:
            ex = get_by_name(s, f"/roles/name/role-{role}?fields=policies")
            if ex:
                out[role] = ex["id"]
    return out


def seed_teams(s, user_ids: Dict[str, str], role_ids: Dict[str, str]) -> Dict[str, str]:
    print("--- Teams ---")
    out: Dict[str, str] = {}
    for role in ROLES:
        tname = f"team-{role}"
        member_uuids = [user_ids[u] for u in TEAM_MEMBERS[role] if u in user_ids]
        existing = get_by_name(s, f"/teams/name/{tname}?fields=users,defaultRoles")
        if existing:
            ops = []
            cur_users = sorted(u["id"] for u in existing.get("users") or [])
            cur_roles = sorted(r["id"] for r in existing.get("defaultRoles") or [])
            want_users = sorted(member_uuids)
            want_roles = sorted([role_ids[role]])
            if cur_users != want_users:
                ops.append({"op": "replace" if existing.get("users") else "add",
                            "path": "/users",
                            "value": [{"id": u, "type": "user"} for u in member_uuids]})
            if cur_roles != want_roles:
                ops.append({"op": "replace" if existing.get("defaultRoles") else "add",
                            "path": "/defaultRoles",
                            "value": [{"id": role_ids[role], "type": "role"}]})
            if ops:
                patch_entity(s, f"/teams/{existing['id']}", ops, f"team {tname} (PATCH)")
            else:
                print(f"  ✓ team {tname} (already up to date)")
            out[role] = existing["id"]
        else:
            body = {
                "name": tname,
                "displayName": f"Eunomia {role}",
                "teamType": "Group",
                "users": member_uuids,
                "defaultRoles": [role_ids[role]],
            }
            r = put(s, "/teams", body, f"team {tname} (create)")
            if r:
                out[role] = r["id"]
    return out


def tag_views(s):
    """PATCH each view's tags so deny-by-tag policy can match."""
    print("--- View tags ---")
    for view_name, roles in VIEW_ACCESS.items():
        fqn = f"{SCHEMA_FQN}.{view_name}"
        view = get_by_name(s, f"/tables/name/{fqn}?fields=tags")
        if not view:
            print(f"  ✗ view {view_name} not found")
            continue
        existing_tags = view.get("tags") or []
        wanted_fqns = {f"{TAG_CLASSIFICATION}.{r}" for r in roles}
        existing_fqns = {t.get("tagFQN") for t in existing_tags}
        if wanted_fqns.issubset(existing_fqns):
            print(f"  ✓ {view_name} already tagged: {sorted(wanted_fqns)}")
            continue
        # We keep any unrelated existing tags (e.g., PII.Sensitive on columns)
        # and add only the access tags we want. Replace the table-level tags.
        new_tags = [t for t in existing_tags if t.get("tagFQN") not in {
            f"{TAG_CLASSIFICATION}.{r}" for r in ROLES
        }]
        for fqn in wanted_fqns:
            new_tags.append({"tagFQN": fqn, "source": "Classification", "labelType": "Manual",
                             "state": "Confirmed"})
        op = "replace" if existing_tags else "add"
        ops = [{"op": op, "path": "/tags", "value": new_tags}]
        patch_entity(s, f"/tables/{view['id']}", ops, f"{view_name} tags={sorted(wanted_fqns)}")


def main() -> int:
    print("Acquiring Keycloak admin token (om.admin)…")
    token = keycloak_admin_token()
    s = om_session(token)
    me = s.get(f"{OM_URL}/users/loggedInUser").json()
    print(f"  whoami={me.get('email')} isAdmin={me.get('isAdmin')}\n")

    user_ids = seed_users(s)
    print()

    if not seed_classification_and_tags(s):
        return 1
    print()

    policy_ids = seed_policies(s)
    if not policy_ids:
        return 1
    print()

    role_ids = seed_roles(s, policy_ids)
    if not role_ids:
        return 1
    print()

    team_ids = seed_teams(s, user_ids, role_ids)
    print()

    tag_views(s)
    print()
    print("Seeding complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
