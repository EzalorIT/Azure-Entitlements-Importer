import os, json, requests, subprocess
import click
from pathlib import Path
from msal import ConfidentialClientApplication

GRAPH_API = "https://graph.microsoft.com/beta"

def get_token(client_id, client_secret, tenant_id):
    app = ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=f"https://login.microsoftonline.com/{tenant_id}"
    )
    token = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    return token['access_token']

def get_all(url, headers):
    items = []
    while url:
        res = requests.get(url, headers=headers).json()
        items += res.get("value", [])
        url = res.get("@odata.nextLink")
    return items

def get_display_name(headers, object_id):
    url = f"{GRAPH_API}/directoryObjects/{object_id}"
    res = requests.get(url, headers=headers).json()
    return res.get("displayName", "UnknownGroup")

def safe(name):
    return name.replace(" ", "_").replace("-", "_").lower()

@click.command()
@click.option('--client-id', required=True)
@click.option('--client-secret', required=True)
@click.option('--tenant-id', required=True)
@click.option('--catalog-id', required=True)
@click.option('--out-dir', default='./iac', show_default=True)
@click.option('--grouped-tfvars', is_flag=True, help="Generate group-based tfvars")
@click.option('--generate-main', is_flag=True, help="Create main.tf and variables.tf")
@click.option('--run-imports', is_flag=True, help="Run the terraform_import.sh script")
def main(client_id, client_secret, tenant_id, catalog_id, out_dir, grouped_tfvars, generate_main, run_imports):
    os.makedirs(out_dir, exist_ok=True)
    os.chdir(out_dir)

    access_token = get_token(client_id, client_secret, tenant_id)
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    print("Fetching access packages...")
    packages = get_all(f"{GRAPH_API}/identityGovernance/entitlementManagement/accessPackages?$filter=catalogId eq '{catalog_id}'", headers)

    access_packages, assignment_policies, assignments = {}, {}, {}
    grouped_assignments, import_cmds = {}, []

    for pkg in packages:
        name = safe(pkg["displayName"])
        access_packages[name] = {
            "catalog_id": pkg["catalogId"],
            "description": pkg.get("description", "")
        }
        import_cmds.append(f'terraform import azuread_access_package.{name} {pkg["id"]}')

        pols = get_all(f"{GRAPH_API}/identityGovernance/entitlementManagement/accessPackages/{pkg['id']}/assignmentPolicies", headers)
        for pol in pols:
            pol_key = f"{name}_{safe(pol['displayName'])}"
            assignment_policies[pol_key] = {
                "access_package_key": name,
                "duration_in_days": pol.get("durationInDays", 30),
                "request_approval": pol.get("requestApprovalSettings", {}).get("isApprovalRequired", False),
                "approvers_primary": [
                    a.get("id") for stage in pol.get("requestApprovalSettings", {}).get("approvalStages", [])
                    for a in stage.get("primaryApprovers", [])
                ],
                "approvers_secondary": [
                    a.get("id") for stage in pol.get("requestApprovalSettings", {}).get("approvalStages", [])
                    for a in stage.get("escalationApprovers", [])
                ]
            }
            import_cmds.append(f'terraform import azuread_access_package_assignment_policy.{pol_key} {pol["id"]}')

        assigns = get_all(f"{GRAPH_API}/identityGovernance/entitlementManagement/accessPackageAssignments?$filter=accessPackageId eq '{pkg['id']}'", headers)
        for a in assigns:
            target_id = a["targetId"]
            group = safe(get_display_name(headers, target_id))
            assign_key = f"{name}_user_{target_id[:6]}"
            assignment = {
                "access_package_key": name,
                "target_id": target_id
            }
            assignments[assign_key] = assignment
            import_cmds.append(f'terraform import azuread_access_package_assignment.{assign_key} {a["id"]}')
            if grouped_tfvars:
                grouped_assignments.setdefault(group, {})[assign_key] = assignment

    with open("terraform.tfvars", "w") as f:
        f.write("access_packages = "); json.dump(access_packages, f, indent=2); f.write("\n\n")
        f.write("assignment_policies = "); json.dump(assignment_policies, f, indent=2); f.write("\n\n")
        f.write("assignments = "); json.dump(assignments, f, indent=2)
        print("✅ terraform.tfvars written")

    if grouped_tfvars:
        for group, assigns in grouped_assignments.items():
            with open(f"{group}.tfvars", "w") as f:
                f.write("assignments = "); json.dump(assigns, f, indent=2)
                print(f"✅ {group}.tfvars written")

    with open("terraform_import.sh", "w") as f:
        f.write("#!/bin/bash\n\n")
        for cmd in import_cmds:
            f.write(cmd + "\n")
        print("✅ terraform_import.sh written")

    if generate_main and not Path("main.tf").exists():
        with open("main.tf", "w") as f:
            f.write("""resource "azuread_access_package" "packages" {
  for_each     = var.access_packages
  display_name = each.key
  catalog_id   = each.value.catalog_id
  description  = each.value.description
}

resource "azuread_access_package_assignment_policy" "policies" {
  for_each             = var.assignment_policies
  display_name         = each.key
  access_package_id    = azuread_access_package.packages[each.value.access_package_key].id
  duration_in_days     = each.value.duration_in_days
  request_approval     = each.value.request_approval

  approval_settings {
    approval_mode  = "Serial"
    stages = [
      {
        primary_approvers    = each.value.approvers_primary
        escalation_approvers = each.value.approvers_secondary
        duration_in_days     = 7
      }
    ]
  }

  requestor_settings {
    scope_type = "AllExistingConnectedOrganizationSubjects"
  }
}

resource "azuread_access_package_assignment" "assignments" {
  for_each          = var.assignments
  access_package_id = azuread_access_package.packages[each.value.access_package_key].id
  target_id         = each.value.target_id
}
""")
            print("✅ main.tf scaffold created")

    if generate_main and not Path("variables.tf").exists():
        with open("variables.tf", "w") as f:
            f.write("""variable "access_packages" {
  type = map(object({
    catalog_id   = string
    description  = string
  }))
}

variable "assignment_policies" {
  type = map(object({
    access_package_key  = string
    duration_in_days    = number
    request_approval    = bool
    approvers_primary   = list(string)
    approvers_secondary = list(string)
  }))
}

variable "assignments" {
  type = map(object({
    access_package_key = string
    target_id          = string
  }))
}
""")
            print("✅ variables.tf scaffold created")

    if run_imports:
        if Path("backend.tf").exists() or (Path("main.tf").exists() and "backend" in Path("main.tf").read_text()):
            print("Detected Terraform backend config. Running terraform init...")
            subprocess.run(["terraform", "init"], check=True)
        subprocess.run(["bash", "terraform_import.sh"])
        print("✅ terraform import executed")

if __name__ == '__main__':
    main()
