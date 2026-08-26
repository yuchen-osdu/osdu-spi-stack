// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// AKS-native Flux extension and cluster-scoped GitOps configuration. The CLI
// installs the extension first, then activates reconciliation after bootstrap
// creates the configured GitOps namespace and inputs.

targetScope = 'resourceGroup'

@description('Name of the AKS cluster where Flux is installed as a cluster-scoped extension.')
param clusterName string

@description('HTTPS URL of the Git repository that Flux reconciles.')
param repoUrl string

@description('Git branch that Flux reconciles.')
param repoBranch string = 'main'

@description('Allowed profile directory under software/stacks/osdu/profiles.')
@allowed([
  'bare'
  'minimal'
  'core'
  'graduated'
])
param profile string = 'core'

@description('Ingress profile path segment under software/stacks/osdu/ingress (for example azure or azure-graduated).')
param ingressMode string = 'azure'

@description('Resource name for the cluster Flux configuration.')
param configurationName string = 'osdu-spi-stack-system'

@description('Create the fluxConfigurations GitOps resource. Set false to install only the Flux extension and namespace.')
param activateGitOps bool = true

@description('Namespace for SPI-owned GitRepository, Kustomizations, HelmReleases, and bootstrap ConfigMaps.')
param gitopsNamespace string = 'osdu-flux'

@description('Optional local Kubernetes Secret name for private Git repository auth.')
param gitRepositoryLocalAuthRef string = ''

var gitRepositoryBase = {
  url: repoUrl
  repositoryRef: {
    branch: repoBranch
  }
  syncIntervalInSeconds: 600
  timeoutInSeconds: 600
}

var gitRepositoryAuth = !empty(gitRepositoryLocalAuthRef) ? {
  localAuthRef: gitRepositoryLocalAuthRef
} : {}

var ingressPath = './software/stacks/osdu/ingress/${ingressMode}'

resource aks 'Microsoft.ContainerService/managedClusters@2024-10-01' existing = {
  name: clusterName
}

resource fluxExtension 'Microsoft.KubernetesConfiguration/extensions@2024-11-01' = {
  name: 'flux'
  scope: aks
  properties: {
    extensionType: 'microsoft.flux'
    autoUpgradeMinorVersion: true
    releaseTrain: 'Stable'
    // Multi-tenancy enforcement injects flux-applier impersonation, which AKS
    // Automatic admission policy rejects with `dry-run failed (Forbidden)`.
    // Disabling it lets controllers apply as their exempt flux-system identities.
    configurationSettings: {
      'multiTenancy.enforce': 'false'
    }
    scope: {
      cluster: {
        releaseNamespace: 'flux-system'
      }
    }
  }
}

resource gitopsConfig 'Microsoft.KubernetesConfiguration/fluxConfigurations@2024-11-01' = if (activateGitOps) {
  name: configurationName
  scope: aks
  properties: {
    scope: 'cluster'
    // AKS Automatic denies deployer writes to flux-system. The CLI seeds
    // SPI-owned inputs into the configured namespace, so reconciliation uses it.
    namespace: gitopsNamespace
    sourceKind: 'GitRepository'
    gitRepository: union(gitRepositoryBase, gitRepositoryAuth)
    kustomizations: {
      stack: {
        path: './software/stacks/osdu/profiles/${profile}'
        prune: true
        syncIntervalInSeconds: 600
        timeoutInSeconds: 1800
      }
      ingress: {
        path: ingressPath
        prune: true
        syncIntervalInSeconds: 600
        timeoutInSeconds: 1800
      }
    }
  }
  dependsOn: [
    fluxExtension
  ]
}

@description('Resource name of the deployed Flux configuration, or an empty string when activation is disabled.')
output configurationName string = activateGitOps ? gitopsConfig.name : ''

@description('Resource name of the deployed Flux extension.')
output extensionName string = fluxExtension.name
