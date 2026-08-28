# ADR-012: Three Ingress Profiles

## Context

Developer environments need an Azure-assigned hostname, team environments may
own an Azure DNS zone, and diagnostic environments may require direct HTTP by
IP. These cases differ in certificate, DNS, Gateway listener, and route
requirements; one conditional tree obscures ownership and dependency edges.

## Decision

Select one self-contained Flux tree under
`software/stacks/osdu/ingress/<mode>/` through `spi up --ingress-mode`.

| Mode | Address | TLS and DNS |
|---|---|---|
| `azure` | `<label>.<region>.cloudapp.azure.com` | Let's Encrypt HTTP-01, no ExternalDNS |
| `dns` | service hosts under the selected zone | Let's Encrypt HTTP-01 and ExternalDNS |
| `ip` | ingress IP | HTTP only |

`azure` is the default. Each tree is the sole Flux owner of
`Gateway/aks-istio-ingress/spi-gateway` and binds it to the AKS managed Istio
ingress Service. `spi-ingress-config` in `osdu-flux` supplies the selected
mode's hostname, address, identity, and certificate substitution values.

The `minimal` profile selects `<mode>-minimal` trees without OSDU routes.
The `graduated` profile selects `<mode>-graduated` trees that add the Wellbore
route. The `bare` profile selects the empty ingress tree and rejects explicit
ingress options.

Rejected: one conditional tree reduces directory count, but hides object ownership and dependency differences between modes.

Rejected: Azure DNS for every deployment provides uniform host names, but requires each subscription to own a zone.

## Consequences

- Switching mode changes the Flux ingress path and can replace listeners,
  certificates, and DNS resources.
- The `dns` mode adds a UAMI and DNS Zone Contributor assignment; `ip` omits
  TLS and middleware UI routes.
- A new mode requires a complete ingress tree, which increases duplication but
  keeps each rendered topology reviewable.
