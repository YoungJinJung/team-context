"""Create local development resources; existing data is retained."""
from botocore.exceptions import ClientError
from storage import clients


def initialize():
    s3, db = clients()
    try:
        s3.head_bucket(Bucket='team-context-records')
    except ClientError as error:
        if error.response['Error']['Code'] not in ('404', 'NoSuchBucket', 'NotFound'):
            raise
        s3.create_bucket(Bucket='team-context-records')
    try:
        db.describe_table(TableName='team-context-sync')
    except db.exceptions.ResourceNotFoundException:
        db.create_table(TableName='team-context-sync', KeySchema=[{'AttributeName': 'connection_id', 'KeyType': 'HASH'}], AttributeDefinitions=[{'AttributeName': 'connection_id', 'AttributeType': 'S'}], BillingMode='PAY_PER_REQUEST')
        db.get_waiter('table_exists').wait(TableName='team-context-sync')


if __name__ == '__main__':
    initialize()
    print('Local S3 bucket and DynamoDB table are ready.')
