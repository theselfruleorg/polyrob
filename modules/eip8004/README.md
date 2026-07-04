# ERC-8004: Trustless Agents Implementation

> **Standard**: [EIP-8004](https://eips.ethereum.org/EIPS/eip-8004) (Draft)  
> **Authors**: Marco De Rossi, Davide Crapis, Jordan Ellis, Erik Reppel  
> **Status**: Review  
> **Created**: 2025-08-13

This module implements the ERC-8004 standard for trustless agent discovery and trust, enabling open-ended agent economies without pre-existing organizational relationships.

---

## Table of Contents

- [Overview](#overview)
- [Why ERC-8004?](#why-erc-8004)
- [Architecture](#architecture)
- [The Three Registries](#the-three-registries)
- [Registration File Specification](#registration-file-specification)
- [Integration with POLYROB](#integration-with-rob)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [Trust Flow Examples](#trust-flow-examples)
- [Smart Contract ABIs](#smart-contract-abis)
- [Module Structure](#module-structure)
- [Testing](#testing)
- [Security Considerations](#security-considerations)
- [References](#references)

---

## Overview

ERC-8004 enables agents to **discover, choose, and interact** with other agents across organizational boundaries without pre-existing trust. It creates a trust layer on top of existing agent communication protocols (A2A, MCP).

### Key Features

| Feature | Description |
|---------|-------------|
| **Decentralized Identity** | NFT-based (ERC-721) agent identity on any EVM chain |
| **Portable Reputation** | On-chain feedback scores that follow agents across platforms |
| **Pluggable Trust** | Choose from reputation, crypto-economic, or TEE validation |
| **Protocol Agnostic** | Works with A2A, MCP, or any future agent protocol |
| **Payment Integration** | x402 payment proofs enrich feedback signals |

---

## Why ERC-8004?

### The Problem

Agent protocols like **MCP** (Model Context Protocol) and **A2A** (Agent-to-Agent) handle communication and task execution, but they don't answer:

- **How do I find agents?** (Discovery)
- **Can I trust this agent?** (Trust)
- **Is this agent's work correct?** (Validation)

### The Solution

ERC-8004 provides three lightweight on-chain registries:

```
┌─────────────────────────────────────────────────────────────────┐
│                    ERC-8004 Trust Layer                         │
├─────────────────┬─────────────────────┬─────────────────────────┤
│ Identity        │ Reputation          │ Validation              │
│ Registry        │ Registry            │ Registry                │
│                 │                     │                         │
│ "Who is this    │ "How good is        │ "Is this work           │
│  agent?"        │  this agent?"       │  correct?"              │
├─────────────────┴─────────────────────┴─────────────────────────┤
│              Agent Communication Protocols                       │
│                    (A2A, MCP, etc.)                              │
└─────────────────────────────────────────────────────────────────┘
```

---

## Architecture

### System Overview

```
                                 ┌──────────────────┐
                                 │  On-Chain        │
                                 │  (Base, Ethereum)│
                                 │                  │
                                 │ ┌──────────────┐ │
        ┌────────────────────────┼─│   Identity   │ │
        │                        │ │   Registry   │ │
        │                        │ │  (ERC-721)   │ │
        │                        │ └──────────────┘ │
        │                        │                  │
        │ tokenURI               │ ┌──────────────┐ │
        ▼                        │ │  Reputation  │◄┼──── Feedback
┌───────────────────┐            │ │   Registry   │ │
│                   │            │ └──────────────┘ │
│  Registration     │            │                  │
│  File (JSON)      │            │ ┌──────────────┐ │
│                   │            │ │  Validation  │◄┼──── Verify
│  • name           │            │ │   Registry   │ │
│  • description    │            │ └──────────────┘ │
│  • endpoints[]    │            └──────────────────┘
│  • registrations[]│
│  • supportedTrust │
│                   │
└───────┬───────────┘
        │
        │ Links to
        ▼
┌───────────────────┐     ┌───────────────────┐
│                   │     │                   │
│  A2A Agent Card   │     │  MCP Endpoint     │
│  /.well-known/    │     │  /mcp             │
│  agent.json       │     │                   │
│                   │     │                   │
└───────────────────┘     └───────────────────┘
```

### Data Flow

```
1. Client queries Identity Registry for agent NFT
2. Gets tokenURI → resolves to registration.json
3. Registration file lists endpoints (A2A, MCP, wallets)
4. Client connects via preferred protocol (A2A)
5. After task completion, agent signs feedback authorization
6. Client submits feedback to Reputation Registry
7. Optional: Request independent validation
```

---

## The Three Registries

### 1. Identity Registry (ERC-721)

An NFT-based registry where each agent has a unique tokenId. The tokenURI resolves to a registration file containing all agent metadata.

**Key Functions:**

```solidity
// Register a new agent
function register(string tokenURI) returns (uint256 agentId)

// Get registration file URI
function tokenURI(uint256 agentId) returns (string)

// Get on-chain metadata
function getMetadata(uint256 agentId, string key) returns (bytes)

// Set on-chain metadata
function setMetadata(uint256 agentId, string key, bytes value)
```

**Agent Identification:**
```
namespace:chainId:identityRegistry:agentId
Example: eip155:8453:0x1234...abcd:42
```

### 2. Reputation Registry

An on-chain feedback system where clients score agents (0-100) after task completion.

**Key Features:**

- **Authorization Required**: Agent must sign a `feedbackAuth` to authorize specific clients
- **Tags for Filtering**: `tag1` and `tag2` fields for categorization (e.g., "web-automation", "research")
- **Off-chain Details**: `fileUri` points to IPFS/HTTPS with full feedback context
- **x402 Integration**: Payment proofs can enrich feedback signals

**Key Functions:**

```solidity
// Submit feedback (requires feedbackAuth signature)
function submitFeedback(
    uint256 agentId,
    bytes feedbackAuth,
    uint8 score,           // 0-100
    bytes32 tag1,
    bytes32 tag2,
    string fileUri,
    bytes32 fileHash
)

// Get aggregated feedback
function getAggregatedFeedback(
    uint256 agentId,
    address clientAddress,  // Optional filter
    bytes32 tag1,           // Optional filter
    bytes32 tag2            // Optional filter
) returns (
    uint64 count,
    uint8 avgScore,
    uint8[] scores,
    bytes32[] tag1s,
    bytes32[] tag2s,
    bool[] revokedStatuses
)

// Revoke feedback (by client only)
function revokeFeedback(uint256 agentId, uint64 feedbackIndex)
```

### 3. Validation Registry

Enables independent verification of agent work through various trust models.

**Validation Types:**

| Type | Description | Use Case |
|------|-------------|----------|
| `stake-secured` | Re-execution by staked validators | High-value tasks |
| `zkml` | Zero-knowledge ML proofs | Verifiable inference |
| `tee` | Trusted Execution Environment attestation | Confidential compute |
| `judge` | Trusted third-party arbitration | Disputes |

**Key Functions:**

```solidity
// Request validation
function validationRequest(
    address validatorAddress,
    uint256 agentId,
    string requestUri,      // Data for validator
    bytes32 requestHash
)

// Submit response (validator only)
function validationResponse(
    bytes32 requestHash,
    uint8 response,         // 0-100
    string responseUri,
    bytes32 responseHash,
    bytes32 tag
)

// Get validation status
function getValidationStatus(bytes32 requestHash) returns (
    address validatorAddress,
    uint256 agentId,
    uint8 response,
    bytes32 tag,
    uint256 lastUpdate
)
```

---

## Registration File Specification

The registration file is a JSON document that the Identity Registry's `tokenURI` resolves to.

### Full Schema

```json
{
  "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
  "name": "POLYROB",
  "description": "AI automation agent with browser control, file system access, MCP integrations, and autonomous task execution capabilities.",
  "image": "https://your-domain.example/static/images/rob-logo.png",
  
  "endpoints": [
    {
      "name": "A2A",
      "endpoint": "https://your-domain.example/.well-known/agent.json",
      "version": "1.0"
    },
    {
      "name": "MCP",
      "endpoint": "https://your-domain.example/mcp",
      "version": "2025-06-18",
      "capabilities": {
        "tools": true,
        "resources": true,
        "prompts": true
      }
    },
    {
      "name": "agentWallet",
      "endpoint": "eip155:8453:0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb7"
    },
    {
      "name": "x402",
      "endpoint": "https://your-domain.example/api/x402/pricing",
      "version": "1.0"
    },
    {
      "name": "EIP8004-reputation",
      "endpoint": "https://your-domain.example/eip8004/reputation",
      "version": "1.0"
    },
    {
      "name": "EIP8004-validation",
      "endpoint": "https://your-domain.example/eip8004/validation",
      "version": "1.0"
    },
    {
      "name": "ENS",
      "endpoint": "rob.eth",
      "version": "v1"
    },
    {
      "name": "DID",
      "endpoint": "did:ethr:0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb7",
      "version": "v1"
    }
  ],
  
  "registrations": [
    {
      "agentId": 1,
      "agentRegistry": "eip155:8453:0xIdentityRegistryAddress..."
    }
  ],
  
  "supportedTrust": [
    "reputation",
    "crypto-economic",
    "tee-attestation"
  ]
}
```

### Field Descriptions

| Field | Required | Description |
|-------|----------|-------------|
| `type` | Yes | Schema identifier, must be `https://eips.ethereum.org/EIPS/eip-8004#registration-v1` |
| `name` | Yes | Agent name (ERC-721 compatible) |
| `description` | Yes | Human-readable description of agent capabilities |
| `image` | No | Agent avatar/logo URL (ERC-721 compatible) |
| `endpoints` | Yes | Array of protocol endpoints |
| `registrations` | Should | On-chain registration references |
| `supportedTrust` | No | Trust models this agent supports |

### Endpoint Types

| Name | Description | Example |
|------|-------------|---------|
| `A2A` | Agent-to-Agent protocol endpoint | `/.well-known/agent.json` |
| `MCP` | Model Context Protocol endpoint | `/mcp` |
| `agentWallet` | Agent's payment wallet (CAIP-10 format) | `eip155:8453:0x...` |
| `x402` | x402 payment pricing endpoint | `/api/x402/pricing` |
| `ENS` | Ethereum Name Service name | `agent.eth` |
| `DID` | Decentralized Identifier | `did:ethr:0x...` |

---

## Integration with POLYROB

POLYROB implements ERC-8004 on top of existing protocols:

```
┌─────────────────────────────────────────────────────────────────┐
│                          POLYROB Agent                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │   ERC-8004  │  │    A2A      │  │         x402            │  │
│  │   Identity  │  │   Protocol  │  │       Payments          │  │
│  │ + Reputation│  │             │  │                         │  │
│  │ + Validation│  │ Agent Card  │  │  Pay-per-request        │  │
│  │             │  │ Task Mgmt   │  │  USDC/ETH on Base       │  │
│  │             │  │ Streaming   │  │                         │  │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘  │
│         │                │                     │                 │
│         └────────────────┼─────────────────────┘                 │
│                          │                                       │
│                          ▼                                       │
│               ┌─────────────────────┐                            │
│               │  Registration File  │                            │
│               │  Links all together │                            │
│               └─────────────────────┘                            │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Existing Capabilities

| Protocol | Status | Endpoint |
|----------|--------|----------|
| **A2A** | ✅ Implemented | `/.well-known/agent.json` |
| **x402** | ✅ Implemented | `/api/x402/pricing` |
| **MCP** | ✅ Implemented | `/mcp` (via MCP servers) |

### New ERC-8004 Capabilities

| Component | Status | Endpoint |
|-----------|--------|----------|
| Registration | ✅ Implemented | `/eip8004/registration.json` |
| Reputation | ✅ Implemented | `/eip8004/reputation/*` |
| Validation | ✅ Implemented | `/eip8004/validation/*` |

---

## Configuration

### Environment Variables

Add these to your `.env` file:

```bash
# ============================================
# ERC-8004 TRUSTLESS AGENTS
# ============================================

# Enable/Disable ERC-8004 integration
EIP8004_ENABLED=true

# Blockchain Configuration
EIP8004_CHAIN_ID=8453                    # Base Mainnet (default)
# Other options: 1 (Ethereum), 137 (Polygon), 42161 (Arbitrum)

# Contract Addresses (deploy your own or use shared registries)
EIP8004_IDENTITY_REGISTRY=0x...          # Identity Registry contract
EIP8004_REPUTATION_REGISTRY=0x...        # Reputation Registry contract  
EIP8004_VALIDATION_REGISTRY=0x...        # Validation Registry contract

# Agent Identity (after minting your NFT)
EIP8004_AGENT_ID=1                       # Your agent's tokenId
EIP8004_AGENT_WALLET=0x...               # Agent's wallet address

# Agent Private Key for EIP-712 Signing (KEEP SECRET!)
# Used to sign feedback authorizations
EIP8004_AGENT_PRIVATE_KEY=0x...

# Supported Trust Models (comma-separated)
# Options: reputation, crypto-economic, tee-attestation
EIP8004_SUPPORTED_TRUST=reputation,crypto-economic
```

### Configuration Model

```python
from modules.eip8004.models import EIP8004Config

config = EIP8004Config(
    enabled=True,
    chain_id=8453,
    identity_registry_address="0x...",
    reputation_registry_address="0x...",
    validation_registry_address="0x...",
    agent_id=1,
    agent_wallet="0x...",
    supported_trust=["reputation", "crypto-economic"],
)
```

---

## API Reference

### Discovery Endpoints

#### GET `/eip8004/registration.json`

Returns the ERC-8004 registration file.

**Response:**
```json
{
  "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
  "name": "POLYROB",
  "description": "AI automation agent...",
  "endpoints": [...],
  "registrations": [...],
  "supportedTrust": ["reputation"]
}
```

#### GET `/eip8004/config`

Returns current configuration status.

**Response:**
```json
{
  "enabled": true,
  "chainId": 8453,
  "agentId": 1,
  "identityRegistry": "0x...",
  "reputationRegistry": "0x...",
  "validationRegistry": "0x...",
  "supportedTrust": ["reputation", "crypto-economic"]
}
```

### Reputation Endpoints

#### POST `/eip8004/reputation/authorize`

Create a signed feedback authorization. Called by agent after accepting a task.

**Request:**
```json
{
  "clientAddress": "0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb7",
  "taskId": "a2a-task-uuid",
  "expiresInSeconds": 86400
}
```

**Response:**
```json
{
  "agentId": 1,
  "clientAddress": "0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb7",
  "expiresAt": 1734123456,
  "nonce": "abc123def456",
  "signature": "0x...",
  "message": "Feedback authorization created"
}
```

#### POST `/eip8004/reputation/feedback`

Submit feedback for an agent. Requires valid authorization.

**Request:**
```json
{
  "agentId": 1,
  "score": 95,
  "feedbackAuth": {
    "agentId": 1,
    "clientAddress": "0x...",
    "expiresAt": 1734123456,
    "nonce": "abc123def456",
    "signature": "0x..."
  },
  "tag1": "web-automation",
  "tag2": "browser-control",
  "skill": "web-automation",
  "taskId": "a2a-task-uuid",
  "comment": "Excellent work on the web scraping task",
  "proofOfPayment": {
    "fromAddress": "0x...",
    "toAddress": "0x...",
    "chainId": "8453",
    "txHash": "0x..."
  }
}
```

**Response:**
```json
{
  "success": true,
  "fileHash": "0x...",
  "agentId": 1,
  "score": 95,
  "message": "Feedback recorded"
}
```

#### GET `/eip8004/reputation/{agent_id}`

Get reputation summary and feedback list.

**Response:**
```json
{
  "summary": {
    "agentId": 1,
    "totalFeedback": 42,
    "averageScore": 94.5,
    "recentScores": [95, 92, 98, 90, 95],
    "topTags": ["web-automation", "research", "file-management"]
  },
  "feedback": [
    {
      "score": 95,
      "clientAddress": "0x...",
      "tag1": "web-automation",
      "tag2": "browser-control",
      "fileHash": "0x..."
    }
  ]
}
```

#### POST `/eip8004/reputation/query`

Query reputation with filters.

**Request:**
```json
{
  "agentId": 1,
  "clientAddress": "0x...",
  "tag1": "web-automation",
  "tag2": null
}
```

### Validation Endpoints

#### POST `/eip8004/validation/request`

Request validation from a validator.

**Request:**
```json
{
  "validatorAddress": "0xValidatorContract...",
  "requestData": {
    "taskId": "a2a-task-uuid",
    "input": "Navigate to example.com",
    "output": "Screenshot saved",
    "evidence": "ipfs://..."
  }
}
```

**Response:**
```json
{
  "success": true,
  "requestHash": "0x...",
  "validatorAddress": "0x...",
  "agentId": 1,
  "message": "Validation request submitted"
}
```

#### POST `/eip8004/validation/respond`

Submit validation response (for validators).

**Request:**
```json
{
  "requestHash": "0x...",
  "response": 100,
  "responseData": {
    "verified": true,
    "evidence": "Re-execution matched original output"
  },
  "tag": "stake-secured"
}
```

#### GET `/eip8004/validation/status/{request_hash}`

Get validation status.

**Response:**
```json
{
  "validatorAddress": "0x...",
  "agentId": 1,
  "response": 100,
  "tag": "stake-secured",
  "lastUpdate": 1734123456
}
```

#### GET `/eip8004/validation/summary/{agent_id}`

Get validation summary for an agent.

**Response:**
```json
{
  "agentId": 1,
  "totalValidations": 15,
  "averageResponse": 98.5,
  "validatorBreakdown": {
    "0xValidator1...": 10,
    "0xValidator2...": 5
  }
}
```

#### GET `/eip8004/validation/validators`

List supported validator types.

**Response:**
```json
{
  "stake-secured": "Validation via stake-secured inference re-execution",
  "zkml": "Validation via zkML cryptographic proofs",
  "tee": "Validation via TEE (Trusted Execution Environment) attestation",
  "judge": "Validation via trusted third-party judges"
}
```

---

## Trust Flow Examples

### Example 1: Basic Reputation Flow

```
Client                          POLYROB Agent                    Reputation Registry
   │                                │                               │
   │ 1. Send A2A task              │                               │
   │───────────────────────────────>│                               │
   │                                │                               │
   │ 2. Task accepted              │                               │
   │<───────────────────────────────│                               │
   │                                │                               │
   │ 3. Request feedback auth      │                               │
   │───────────────────────────────>│                               │
   │                                │                               │
   │ 4. Signed feedbackAuth        │                               │
   │<───────────────────────────────│                               │
   │                                │                               │
   │ 5. Task completed             │                               │
   │<───────────────────────────────│                               │
   │                                │                               │
   │ 6. Submit feedback with auth  │                               │
   │───────────────────────────────────────────────────────────────>│
   │                                │                               │
   │ 7. Feedback recorded on-chain │                               │
   │<───────────────────────────────────────────────────────────────│
```

### Example 2: Reputation + x402 Payment Proof

```python
# 1. Client pays via x402
payment = await x402_client.pay(
    amount_usd=0.015,
    recipient="0xAgentWallet...",
    chain="base"
)

# 2. Client sends A2A task
task = await a2a_client.send_message(
    agent_url="https://your-domain.example/a2a",
    message="Research cryptocurrency regulations"
)

# 3. Get feedback authorization from agent
auth = await requests.post(
    "https://your-domain.example/eip8004/reputation/authorize",
    json={"clientAddress": wallet_address}
)

# 4. Submit feedback with payment proof
feedback = await requests.post(
    "https://your-domain.example/eip8004/reputation/feedback",
    json={
        "agentId": 1,
        "score": 95,
        "feedbackAuth": auth.json(),
        "tag1": "research",
        "skill": "research",
        "proofOfPayment": {
            "fromAddress": wallet_address,
            "toAddress": "0xAgentWallet...",
            "chainId": "8453",
            "txHash": payment["txHash"]
        }
    }
)
```

### Example 3: Validation Request

```python
# Request stake-secured validation
validation = await requests.post(
    "https://your-domain.example/eip8004/validation/request",
    json={
        "validatorAddress": "0xStakeSecuredValidator...",
        "requestData": {
            "taskId": task["id"],
            "input": task["input"],
            "output": task["output"],
            "evidenceUri": "ipfs://..."
        }
    }
)

# Check validation status
status = await requests.get(
    f"https://your-domain.example/eip8004/validation/status/{validation['requestHash']}"
)

if status.json()["response"] >= 90:
    print("Task validated successfully!")
```

---

## Smart Contract ABIs

### Identity Registry (ERC-721 + Extensions)

```python
IDENTITY_REGISTRY_ABI = [
    # ERC-721 Standard
    {"name": "tokenURI", "inputs": [{"name": "tokenId", "type": "uint256"}], "outputs": [{"type": "string"}]},
    {"name": "ownerOf", "inputs": [{"name": "tokenId", "type": "uint256"}], "outputs": [{"type": "address"}]},
    
    # ERC-8004 Extensions
    {"name": "getMetadata", "inputs": [{"name": "agentId", "type": "uint256"}, {"name": "key", "type": "string"}], "outputs": [{"type": "bytes"}]},
    {"name": "setMetadata", "inputs": [{"name": "agentId", "type": "uint256"}, {"name": "key", "type": "string"}, {"name": "value", "type": "bytes"}]},
    {"name": "register", "inputs": [{"name": "tokenURI", "type": "string"}], "outputs": [{"name": "agentId", "type": "uint256"}]},
    
    # Events
    {"name": "Registered", "type": "event", "inputs": [
        {"name": "agentId", "type": "uint256", "indexed": True},
        {"name": "tokenURI", "type": "string"},
        {"name": "owner", "type": "address", "indexed": True}
    ]}
]
```

### Reputation Registry

```python
REPUTATION_REGISTRY_ABI = [
    {"name": "getIdentityRegistry", "outputs": [{"name": "identityRegistry", "type": "address"}]},
    {"name": "submitFeedback", "inputs": [
        {"name": "agentId", "type": "uint256"},
        {"name": "feedbackAuth", "type": "bytes"},
        {"name": "score", "type": "uint8"},
        {"name": "tag1", "type": "bytes32"},
        {"name": "tag2", "type": "bytes32"},
        {"name": "fileUri", "type": "string"},
        {"name": "fileHash", "type": "bytes32"}
    ]},
    {"name": "getAggregatedFeedback", "inputs": [
        {"name": "agentId", "type": "uint256"},
        {"name": "clientAddress", "type": "address"},
        {"name": "tag1", "type": "bytes32"},
        {"name": "tag2", "type": "bytes32"}
    ], "outputs": [
        {"name": "count", "type": "uint64"},
        {"name": "avgScore", "type": "uint8"},
        {"name": "scores", "type": "uint8[]"},
        {"name": "tag1s", "type": "bytes32[]"},
        {"name": "tag2s", "type": "bytes32[]"},
        {"name": "revokedStatuses", "type": "bool[]"}
    ]},
    {"name": "getClients", "inputs": [{"name": "agentId", "type": "uint256"}], "outputs": [{"type": "address[]"}]},
    
    # Events
    {"name": "FeedbackSubmitted", "type": "event", "inputs": [
        {"name": "agentId", "type": "uint256", "indexed": True},
        {"name": "clientAddress", "type": "address", "indexed": True},
        {"name": "score", "type": "uint8"},
        {"name": "feedbackIndex", "type": "uint64", "indexed": True}
    ]}
]
```

### Validation Registry

```python
VALIDATION_REGISTRY_ABI = [
    {"name": "getIdentityRegistry", "outputs": [{"name": "identityRegistry", "type": "address"}]},
    {"name": "validationRequest", "inputs": [
        {"name": "validatorAddress", "type": "address"},
        {"name": "agentId", "type": "uint256"},
        {"name": "requestUri", "type": "string"},
        {"name": "requestHash", "type": "bytes32"}
    ]},
    {"name": "validationResponse", "inputs": [
        {"name": "requestHash", "type": "bytes32"},
        {"name": "response", "type": "uint8"},
        {"name": "responseUri", "type": "string"},
        {"name": "responseHash", "type": "bytes32"},
        {"name": "tag", "type": "bytes32"}
    ]},
    {"name": "getValidationStatus", "inputs": [{"name": "requestHash", "type": "bytes32"}], "outputs": [
        {"name": "validatorAddress", "type": "address"},
        {"name": "agentId", "type": "uint256"},
        {"name": "response", "type": "uint8"},
        {"name": "tag", "type": "bytes32"},
        {"name": "lastUpdate", "type": "uint256"}
    ]},
    {"name": "getSummary", "inputs": [
        {"name": "agentId", "type": "uint256"},
        {"name": "validatorAddresses", "type": "address[]"},
        {"name": "tag", "type": "bytes32"}
    ], "outputs": [
        {"name": "count", "type": "uint64"},
        {"name": "avgResponse", "type": "uint8"}
    ]},
    
    # Events
    {"name": "ValidationRequest", "type": "event", "inputs": [
        {"name": "validatorAddress", "type": "address", "indexed": True},
        {"name": "agentId", "type": "uint256", "indexed": True},
        {"name": "requestUri", "type": "string"},
        {"name": "requestHash", "type": "bytes32", "indexed": True}
    ]},
    {"name": "ValidationResponse", "type": "event", "inputs": [
        {"name": "validatorAddress", "type": "address", "indexed": True},
        {"name": "agentId", "type": "uint256", "indexed": True},
        {"name": "requestHash", "type": "bytes32", "indexed": True},
        {"name": "response", "type": "uint8"},
        {"name": "responseUri", "type": "string"},
        {"name": "tag", "type": "bytes32"}
    ]}
]
```

---

## Module Structure

```
modules/eip8004/
├── __init__.py          # Public exports and module metadata
├── models.py            # Pydantic models for all ERC-8004 types
│   ├── EIP8004Config
│   ├── RegistrationFile
│   ├── Endpoint
│   ├── Registration
│   ├── FeedbackAuth
│   ├── FeedbackEntry
│   ├── ProofOfPayment
│   ├── ValidationRequestModel
│   ├── ValidationResponseModel
│   ├── ValidationStatus
│   ├── ValidationSummary
│   └── ReputationSummary
├── registration.py      # Registration file builder
│   ├── get_eip8004_config()
│   ├── build_registration_file()
│   └── get_registration_file_dict()
├── reputation.py        # Reputation manager
│   └── ReputationManager
│       ├── create_feedback_auth()
│       ├── verify_feedback_auth()
│       ├── submit_feedback()
│       ├── get_reputation()
│       └── get_feedback_list()
├── validation.py        # Validation manager
│   └── ValidationManager
│       ├── request_validation()
│       ├── submit_response()
│       ├── get_validation_status()
│       ├── get_validation_summary()
│       ├── list_pending_validations()
│       └── get_supported_validators()
├── contracts.py         # Web3 contract interfaces
│   ├── IDENTITY_REGISTRY_ABI
│   ├── REPUTATION_REGISTRY_ABI
│   ├── VALIDATION_REGISTRY_ABI
│   ├── IdentityRegistryContract
│   ├── ReputationRegistryContract
│   └── ValidationRegistryContract
└── README.md            # This documentation
```

---

## Testing

### Run Module Tests

```bash
cd /path/to/rob_dev
source venv/bin/activate

# Test imports
python -c "from modules.eip8004 import *; print('✅ Imports OK')"

# Test API endpoints
python -c "
from fastapi.testclient import TestClient
from api.app import create_app
client = TestClient(create_app())

# Registration
resp = client.get('/eip8004/registration.json')
assert resp.status_code == 200
print('✅ Registration endpoint OK')

# Config
resp = client.get('/eip8004/config')
assert resp.status_code == 200
print('✅ Config endpoint OK')

# Validators
resp = client.get('/eip8004/validation/validators')
assert resp.status_code == 200
print('✅ Validators endpoint OK')

print('🎉 All tests passed!')
"
```

### Validate Registration File Structure

```bash
curl -s http://localhost:9000/eip8004/registration.json | jq '.'
```

---

## Security Considerations

### Authorization Security

- **feedbackAuth signatures** use EIP-712 typed data signing
- Signatures include nonce to prevent replay attacks
- Expiration timestamps prevent indefinite authorization

### On-Chain Data Integrity

- File hashes (KECCAK-256) ensure off-chain data hasn't been tampered with
- Validation responses can be verified by re-running with the same inputs

### Known Limitations

1. **Sybil Attacks**: Fake agents can inflate reputation with fake reviews
   - Mitigation: Filter by reviewer reputation, x402 payment proofs
   
2. **Centralization Risk**: IPFS gateway or HTTPS server could be unavailable
   - Mitigation: Use content-addressable storage (IPFS) for critical data

3. **Private Key Security**: Agent private key must be kept secure
   - Mitigation: Use environment variables, never commit to code

### Best Practices

```bash
# Store private keys securely
EIP8004_AGENT_PRIVATE_KEY=0x...  # In .env, never in code

# Use content-addressable storage
# IPFS CIDs cannot be tampered with
fileUri: "ipfs://Qm..."

# Include payment proofs when possible
# Paying customers are more credible reviewers
```

---

## References

### Standards & Protocols

- [ERC-8004 Specification](https://eips.ethereum.org/EIPS/eip-8004) - The original EIP
- [A2A Protocol](https://a2a-protocol.org/) - Google's Agent-to-Agent Protocol
- [MCP Protocol](https://modelcontextprotocol.io/) - Anthropic's Model Context Protocol
- [x402 Protocol](https://www.x402.org/) - HTTP 402 Payment Required for AI

### Related EIPs

- [ERC-721](https://eips.ethereum.org/EIPS/eip-721) - NFT Standard (used for Identity Registry)
- [EIP-712](https://eips.ethereum.org/EIPS/eip-712) - Typed Data Signing (used for feedbackAuth)
- [CAIP-10](https://github.com/ChainAgnostic/CAIPs/blob/main/CAIPs/caip-10.md) - Account ID format

### POLYROB Documentation

- [A2A Protocol & API Guide](../../docs/guide/api.md)
- [x402 Payment System](../x402/README.md)
- [API Documentation](../../api/README.md)

---

## License

This implementation follows the ERC-8004 specification which is released under [CC0](https://creativecommons.org/publicdomain/zero/1.0/).

---

*Last updated: December 2024*
