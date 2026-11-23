"""Case store on AWS: documents in S3 (bucket versioning keeps every version), records in DynamoDB.

DynamoDB layout (one table):
  pk = CASE#<case id>
  sk = CASE                        the case itself (status, stage and title copied out for queries)
       RUN#<started at>#<role>#<n> one agent run
       FINDING#<id>                one review finding
       BLOCKER#<id>                one question for the business
       DECISION#<at>#<n>           one gate or escalation decision
Each item keeps the full record as JSON in `data`, which avoids DynamoDB number conversions.

This module imports only the standard library and boto3, so Lambdas can use it too.
"""

from __future__ import annotations

import json
import os

import boto3
from boto3.dynamodb.conditions import Attr, Key

from ..store import CaseStore, check_doc_name


class AwsCaseStore(CaseStore):
    def __init__(self, bucket: str | None = None, table: str | None = None, s3=None, dynamodb=None):
        self.bucket = bucket or os.environ["CASES_BUCKET"]
        table_name = table or os.environ["CASES_TABLE"]
        self.s3 = s3 or boto3.client("s3")
        self.table = (dynamodb or boto3.resource("dynamodb")).Table(table_name)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _pk(case_id: str) -> str:
        return f"CASE#{case_id}"

    def _key(self, case_id: str, name: str) -> str:
        return f"cases/{case_id}/{check_doc_name(name)}"

    def _put(self, case_id: str, sk: str, data: dict, **extra) -> None:
        self.table.put_item(Item={"pk": self._pk(case_id), "sk": sk, "data": json.dumps(data), **extra})

    def _query(self, case_id: str, prefix: str) -> list[dict]:
        items, kwargs = [], {
            "KeyConditionExpression": Key("pk").eq(self._pk(case_id)) & Key("sk").begins_with(prefix)
        }
        while True:
            resp = self.table.query(**kwargs)
            items += [json.loads(i["data"]) for i in resp.get("Items", [])]
            if "LastEvaluatedKey" not in resp:
                return items
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    # --------------------------------------------------------------- case
    def put_case(self, case: dict) -> None:
        extra = {"status": case["status"], "stage": case["stage"], "title": case["title"]}
        token = (case.get("pending") or {}).get("task_token")
        if token:
            extra["task_token"] = token  # top-level copy, so claim_pending can test it atomically
        self._put(case["id"], "CASE", case, **extra)

    def transition_status(self, case_id: str, from_status: str, to_status: str) -> bool:
        """Atomic status change: succeeds only if the case is still in `from_status`."""
        try:
            self.table.update_item(
                Key={"pk": self._pk(case_id), "sk": "CASE"},
                UpdateExpression="SET #s = :to",
                ConditionExpression="#s = :from",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":to": to_status, ":from": from_status},
            )
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def claim_pending(self, case_id: str, token: str) -> bool:
        """Atomically take the waiting task token, so only the first of two simultaneous responses wins."""
        try:
            self.table.update_item(
                Key={"pk": self._pk(case_id), "sk": "CASE"},
                UpdateExpression="REMOVE task_token",
                ConditionExpression="task_token = :t",
                ExpressionAttributeValues={":t": token},
            )
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def get_case(self, case_id: str) -> dict:
        resp = self.table.get_item(Key={"pk": self._pk(case_id), "sk": "CASE"})
        if "Item" not in resp:
            raise KeyError(f"no such case: {case_id}")
        return json.loads(resp["Item"]["data"])

    def list_cases(self) -> list[dict]:
        items, kwargs = [], {"FilterExpression": Attr("sk").eq("CASE")}
        while True:
            resp = self.table.scan(**kwargs)
            items += [json.loads(i["data"]) for i in resp.get("Items", [])]
            if "LastEvaluatedKey" not in resp:
                return sorted(items, key=lambda c: c["created_at"])
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    # ---------------------------------------------------------- documents
    def write_doc(self, case_id: str, name: str, content: str) -> None:
        content_type = "application/json" if name.endswith(".json") else "text/markdown; charset=utf-8"
        self.s3.put_object(Bucket=self.bucket, Key=self._key(case_id, name),
                           Body=content.encode("utf-8"), ContentType=content_type)

    def read_doc(self, case_id: str, name: str) -> str | None:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self._key(case_id, name))
        except self.s3.exceptions.NoSuchKey:
            return None
        return obj["Body"].read().decode("utf-8")

    def list_docs(self, case_id: str, prefix: str = "") -> list[str]:
        base = f"cases/{case_id}/"
        names: list[str] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=base + prefix):
            names += [o["Key"][len(base):] for o in page.get("Contents", [])]
        return sorted(names)

    # ------------------------------------------------------------ records
    def add_run(self, case_id: str, run: dict) -> None:
        n = len(self._query(case_id, "RUN#")) + 1
        self._put(case_id, f"RUN#{run['started_at']}#{run['role']}#{n:04d}", run)

    def list_runs(self, case_id: str) -> list[dict]:
        return self._query(case_id, "RUN#")

    def put_finding(self, case_id: str, finding: dict) -> None:
        self._put(case_id, f"FINDING#{finding['id']}", finding)

    def list_findings(self, case_id: str) -> list[dict]:
        return self._query(case_id, "FINDING#")

    def put_blocker(self, case_id: str, blocker: dict) -> None:
        self._put(case_id, f"BLOCKER#{blocker['id']}", blocker)

    def list_blockers(self, case_id: str) -> list[dict]:
        return self._query(case_id, "BLOCKER#")

    def add_decision(self, case_id: str, decision: dict) -> None:
        n = len(self._query(case_id, "DECISION#")) + 1
        self._put(case_id, f"DECISION#{decision['at']}#{n:04d}", decision)

    def list_decisions(self, case_id: str) -> list[dict]:
        return self._query(case_id, "DECISION#")
