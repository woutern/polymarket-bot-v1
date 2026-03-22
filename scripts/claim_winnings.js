/**
 * Auto-claim redeemable Polymarket winnings via Builder Relayer API.
 *
 * Uses @polymarket/builder-relayer-client with Builder API credentials.
 * Checks for redeemable positions and claims each via the relayer.
 *
 * Usage:
 *   node scripts/claim_winnings.js          # claim all redeemable
 *   node scripts/claim_winnings.js --test   # dry run, just show what's redeemable
 */

const axios = require("axios");
const { createWalletClient, http } = require("viem");
const { privateKeyToAccount } = require("viem/accounts");
const { polygon } = require("viem/chains");
const { encodeFunctionData } = require("viem");

// Load .env FIRST so all env vars are available
try {
    require("dotenv").config();
} catch (e) {
    // dotenv optional — env vars come from ECS Secrets Manager on prod
}

// Config — Builder API credentials
const CHAIN_ID = 137;
const USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";
const CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045";
const NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a";

const POLY_BUILDER_API_KEY = process.env.POLY_BUILDER_API_KEY || "";
const POLY_BUILDER_SECRET = process.env.POLY_BUILDER_SECRET || "";
const POLY_BUILDER_PASSPHRASE = process.env.POLY_BUILDER_PASSPHRASE || "";
const PRIVATE_KEY = process.env.POLYMARKET_PRIVATE_KEY || "";
const FUNDER_ADDRESS = process.env.POLYMARKET_FUNDER || "0x5ca439d661c9b44337E91fC681ec4b006C473610";
const POLYGON_RPC = process.env.POLYGON_RPC || "https://polygon-rpc.com";

const REDEEM_ABI = [
    {
        name: "redeemPositions",
        type: "function",
        inputs: [
            { name: "collateralToken", type: "address" },
            { name: "parentCollectionId", type: "bytes32" },
            { name: "conditionId", type: "bytes32" },
            { name: "indexSets", type: "uint256[]" },
        ],
    },
];

async function getRelayClient() {
    const { RelayClient, RelayerTxType } = await import("@polymarket/builder-relayer-client");
    const { BuilderConfig } = await import("@polymarket/builder-signing-sdk");

    const transport = http(POLYGON_RPC);
    const pk = PRIVATE_KEY.startsWith("0x") ? PRIVATE_KEY : "0x" + PRIVATE_KEY;
    const account = privateKeyToAccount(pk);
    const walletClient = createWalletClient({
        account,
        chain: polygon,
        transport,
    });

    const builderConfig = new BuilderConfig({
        localBuilderCreds: {
            key: POLY_BUILDER_API_KEY,
            secret: POLY_BUILDER_SECRET,
            passphrase: POLY_BUILDER_PASSPHRASE,
        },
    });

    // Our wallet is Gnosis Safe (sig_type=2), try SAFE first
    return new RelayClient(
        "https://relayer-v2.polymarket.com/",
        CHAIN_ID,
        walletClient,
        builderConfig,
        RelayerTxType.SAFE
    );
}

async function claimWinnings(conditionId, negRisk = false) {
    const relayClient = await getRelayClient();

    const targetContract = negRisk ? NEG_RISK_CTF_EXCHANGE : CTF;
    const data = encodeFunctionData({
        abi: REDEEM_ABI,
        functionName: "redeemPositions",
        args: [
            USDC_E,
            "0x0000000000000000000000000000000000000000000000000000000000000000",
            conditionId.startsWith("0x") ? conditionId : "0x" + conditionId,
            [1n, 2n],
        ],
    });

    const redeemTx = {
        to: targetContract,
        data: data,
        value: "0",
    };

    try {
        console.log("  Executing redeem via relayer...");
        const response = await relayClient.execute([redeemTx], "Redeem winnings");
        const result = await response.wait();

        if (result?.transactionHash) {
            console.log("  SUCCESS! Tx:", result.transactionHash);
            return true;
        } else {
            console.log("  Relayer returned no hash:", result);
            return false;
        }
    } catch (error) {
        console.error("  FAILED:", error.message || error);

        // If SAFE fails, try PROXY
        if (error.message && error.message.includes("proxy")) {
            console.log("  Retrying with PROXY type...");
            try {
                const { RelayClient, RelayerTxType } = await import("@polymarket/builder-relayer-client");
                const { BuilderConfig } = await import("@polymarket/builder-signing-sdk");
                const transport = http(POLYGON_RPC);
                const pk = PRIVATE_KEY.startsWith("0x") ? PRIVATE_KEY : "0x" + PRIVATE_KEY;
                const account = privateKeyToAccount(pk);
                const walletClient = createWalletClient({ account, chain: polygon, transport });
                const builderConfig = new BuilderConfig({
                    localBuilderCreds: { key: POLY_BUILDER_API_KEY, secret: POLY_BUILDER_SECRET, passphrase: POLY_BUILDER_PASSPHRASE },
                });
                const proxyClient = new RelayClient("https://relayer-v2.polymarket.com/", CHAIN_ID, walletClient, builderConfig, RelayerTxType.PROXY);
                const resp2 = await proxyClient.execute([redeemTx], "Redeem winnings");
                const result2 = await resp2.wait();
                if (result2?.transactionHash) {
                    console.log("  SUCCESS (PROXY)! Tx:", result2.transactionHash);
                    return true;
                }
            } catch (e2) {
                console.error("  PROXY also failed:", e2.message || e2);
            }
        }
        return false;
    }
}

async function main() {
    const testMode = process.argv.includes("--test");

    if (!PRIVATE_KEY) {
        console.error("ERROR: POLYMARKET_PRIVATE_KEY not set");
        process.exit(1);
    }

    console.log("Checking redeemable positions...");
    console.log("Wallet:", FUNDER_ADDRESS);

    const url = `https://data-api.polymarket.com/positions?user=${FUNDER_ADDRESS}&redeemable=true`;
    const { data: positions } = await axios.get(url).catch(() => ({ data: [] }));

    const redeemable = (positions || []).filter(p => p.redeemable && Number(p.size) > 0);

    if (redeemable.length === 0) {
        console.log("No redeemable positions.");
        return;
    }

    // Dedup by conditionId
    const seen = new Set();
    const unique = [];
    for (const p of redeemable) {
        const cid = p.conditionId || p.condition_id;
        if (!cid || seen.has(cid)) continue;
        seen.add(cid);
        unique.push(p);
    }

    console.log(`Found ${unique.length} redeemable positions:\n`);
    for (const p of unique) {
        const title = (p.title || p.question || "?").slice(0, 50);
        const val = Number(p.currentValue || 0).toFixed(2);
        console.log(`  ${title} | $${val}`);
    }

    if (testMode) {
        console.log("\nDRY RUN — no claims executed.");
        return;
    }

    console.log("\nClaiming...\n");
    let claimed = 0;
    for (const p of unique) {
        const cid = p.conditionId || p.condition_id;
        const title = (p.title || p.question || "?").slice(0, 50);
        const negRisk = p.negativeRisk || p.negative_risk || false;
        console.log(`Claiming: ${title} (${negRisk ? 'negRisk' : 'regular'})`);
        const ok = await claimWinnings(cid, negRisk);
        if (ok) claimed++;
        // Small delay between claims
        await new Promise(r => setTimeout(r, 2000));
    }

    console.log(`\nDone: ${claimed}/${unique.length} claimed.`);
}

main().catch(e => { console.error(e); process.exit(1); });
