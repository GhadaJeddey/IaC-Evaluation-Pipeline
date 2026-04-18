resource "aws_instance" "web" {
  ami           = "ami-0c55b159cbfafe1f0"
  instance_type = "t3.micro"
  subnet_id     = "subnet-00000000000000000"

  tags = {
    Name = "wrong-subnet"
  }
}